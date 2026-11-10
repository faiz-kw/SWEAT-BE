"""
apps/tenant_core/tasks_meta_leads.py - Asynchronous Celery Tasks for Meta Lead Ads Ingestion.

Ensures:
- Fast HTTP 200 return to Meta Webhook gateway by offloading Graph API retrieval to worker
- Bounded exponential retries (1m, 5m, 15m, 30m) for network timeouts and Meta rate limits
- Permanent failure classification (token revoked, lead not found, permissions) with zero retries
- Dead-letter state recording on retry exhaustion
- Complete tenant database isolation via tenant_database_context
- Production-safe structured logging without secret or PII exposure
- Scheduled reconciliation to backfill leads missed during webhook outages
"""
import logging
import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.services_meta_leads import process_live_import
from apps.tenant_core.services_meta_graph import (
    MetaRateLimitError,
    MetaTokenExpiredError,
    MetaLeadNotFoundError,
    MetaPermissionError,
    MetaGraphServerError,
    MetaGraphClient,
)
from apps.tenant_core.meta_logging import (
    log_meta_event,
    EVT_PROCESSING_STARTED,
    EVT_RETRY_SCHEDULED,
    EVT_PROCESSING_FAILED,
    EVT_DEAD_LETTER,
    EVT_LEAD_CREATED,
)
from apps.tenant_core.services_reliability import record_business_audit

logger = logging.getLogger(__name__)

# Centralized Retry Strategy:
# Attempt 1 -> retry after ~1 min (60s)
# Attempt 2 -> retry after ~5 min (300s)
# Attempt 3 -> retry after ~15 min (900s)
# Attempt 4 -> retry after ~30 min (1800s)
META_RETRY_BACKOFF_STEPS = [60, 300, 900, 1800]
META_MAX_RETRIES = 4


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_meta_leads.process_meta_lead_import_task',
    max_retries=META_MAX_RETRIES,
    default_retry_delay=60,
    acks_late=True,
)
def process_meta_lead_import_task(self, tenant_id: str, import_id: str):
    """
    Asynchronously processes a live Meta Lead Ad import event.
    Fetches lead data from Graph API using decrypted Page Access Token,
    resolves branch and salesperson, and commits CRM records.
    """
    if not tenant_id or not import_id:
        logger.error("process_meta_lead_import_task called with missing tenant_id or import_id.")
        return {'status': 'FAILED', 'error': 'tenant_id and import_id are required'}

    current_attempt = self.request.retries + 1

    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_meta_leads import MetaLeadImport
            imp = MetaLeadImport.objects.using(db_alias).select_related('organization').filter(id=import_id).first()
            if not imp:
                logger.warning("MetaLeadImport %s not found in tenant DB %s", import_id, db_alias)
                return {'status': 'FAILED', 'error': 'Import record not found'}

            # Short-circuit if already processed successfully (idempotency safety)
            if imp.status in ('IMPORTED', 'RESOLVED'):
                logger.info("MetaLeadImport %s already completed with status=%s. Skipping execution.", import_id, imp.status)
                return {'status': imp.status, 'lead_id': str(imp.lead_id) if imp.lead_id else None}

            # Update lifecycle state to PROCESSING
            if imp.status in ('PENDING', 'RETRYING'):
                imp.status = 'PROCESSING'
                imp.save(using=db_alias, update_fields=['status', 'updated_at'])

            log_meta_event(
                EVT_PROCESSING_STARTED,
                f"Executing Meta lead import processing (attempt {current_attempt}/{META_MAX_RETRIES})",
                import_id=import_id,
                tenant_id=tenant_id,
                page_id=imp.page_id,
                form_id=imp.form_id,
                leadgen_id=imp.external_lead_id,
                status='PROCESSING',
            )

            event = process_live_import(organization=imp.organization, event_id=import_id, actor_user=None, alias=db_alias)

            if event.status == 'IMPORTED':
                log_meta_event(
                    EVT_LEAD_CREATED,
                    "Meta lead import successfully completed and CRM lead created",
                    import_id=import_id,
                    tenant_id=tenant_id,
                    page_id=event.page_id,
                    form_id=event.form_id,
                    leadgen_id=event.external_lead_id,
                    status='IMPORTED',
                    extra={'lead_id': str(event.lead_id) if event.lead_id else ''},
                )
                # --- FIX: DISPATCH AUTOMATED SARVAM CALL FOR IMPORTED META LEAD START ---
                # WHY: Immediately schedule outbound qualification call for newly ingested Meta lead.
                # FIXES: Connects Meta webhook ingestion pipeline to Sarvam AI voice engine.
                # IMPACT: Automated voice outreach triggers seamlessly subject to consent & calling hours.
                if event.lead_id:
                    try:
                        from apps.tenant_core.tasks_voice_calling import trigger_automated_sarvam_call_task
                        trigger_automated_sarvam_call_task.delay(tenant_id=str(tenant_id), lead_id=str(event.lead_id))
                    except Exception as call_err:
                        logger.warning("Failed to queue automated Sarvam call for lead %s: %s", event.lead_id, call_err)
                # --- FIX: DISPATCH AUTOMATED SARVAM CALL FOR IMPORTED META LEAD END ---
            else:
                log_meta_event(
                    EVT_PROCESSING_FAILED,
                    f"Meta lead import finished with non-imported status: {event.status}",
                    import_id=import_id,
                    tenant_id=tenant_id,
                    page_id=event.page_id,
                    form_id=event.form_id,
                    leadgen_id=event.external_lead_id,
                    status=event.status,
                    error_code=event.error_code,
                )

            return {'status': event.status, 'lead_id': str(event.lead_id) if event.lead_id else None}

    except (MetaTokenExpiredError, MetaPermissionError, MetaLeadNotFoundError) as perm_exc:
        # Permanent failure: Do not retry
        err_type = type(perm_exc).__name__
        logger.error("Permanent Meta error for import %s (%s). Non-retryable: %s", import_id, err_type, perm_exc)
        _record_terminal_failure(tenant_id, import_id, 'FAILED', err_type, str(perm_exc))
        log_meta_event(
            EVT_PROCESSING_FAILED,
            f"Permanent Meta API error: {perm_exc}",
            import_id=import_id,
            tenant_id=tenant_id,
            status='FAILED',
            error_code=err_type,
            level=logging.ERROR,
        )
        return {'status': 'FAILED', 'error': str(perm_exc)}

    except (MetaRateLimitError, MetaGraphServerError, requests.exceptions.RequestException, TimeoutError) as trans_exc:
        # Transient failure: Controlled backoff retry
        err_type = type(trans_exc).__name__
        if self.request.retries < META_MAX_RETRIES:
            countdown = META_RETRY_BACKOFF_STEPS[min(self.request.retries, len(META_RETRY_BACKOFF_STEPS) - 1)]
            logger.warning(
                "Transient Meta error for import %s (%s). Scheduling retry %d/%d in %ds...",
                import_id, err_type, current_attempt, META_MAX_RETRIES, countdown,
            )
            _record_retry_state(tenant_id, import_id, current_attempt, err_type, str(trans_exc))
            log_meta_event(
                EVT_RETRY_SCHEDULED,
                f"Transient error ({err_type}). Retry scheduled in {countdown}s",
                import_id=import_id,
                tenant_id=tenant_id,
                status='RETRYING',
                error_code=err_type,
                extra={'retry_countdown_seconds': countdown, 'attempt': current_attempt},
                level=logging.WARNING,
            )
            raise self.retry(exc=trans_exc, countdown=countdown)
        else:
            # Retries exhausted: Transition to DEAD_LETTER
            dead_letter_msg = f"Exhausted max retries ({current_attempt}/{META_MAX_RETRIES}): {trans_exc}"
            logger.error("Meta lead import %s reached dead-letter state: %s", import_id, dead_letter_msg)
            _record_dead_letter(tenant_id, import_id, current_attempt, 'RETRY_LIMIT_EXCEEDED', dead_letter_msg)
            log_meta_event(
                EVT_DEAD_LETTER,
                dead_letter_msg,
                import_id=import_id,
                tenant_id=tenant_id,
                status='DEAD_LETTER',
                error_code='RETRY_LIMIT_EXCEEDED',
                level=logging.ERROR,
            )
            return {'status': 'DEAD_LETTER', 'error': dead_letter_msg}

    except Exception as general_exc:
        # General unhandled failure: Retry if within limits, otherwise DEAD_LETTER
        err_type = type(general_exc).__name__
        if self.request.retries < META_MAX_RETRIES:
            countdown = META_RETRY_BACKOFF_STEPS[min(self.request.retries, len(META_RETRY_BACKOFF_STEPS) - 1)]
            logger.warning("Unexpected error processing Meta lead %s (%s). Retrying in %ds...", import_id, err_type, countdown)
            _record_retry_state(tenant_id, import_id, current_attempt, err_type, str(general_exc))
            log_meta_event(
                EVT_RETRY_SCHEDULED,
                f"Unexpected error ({err_type}). Retry scheduled in {countdown}s",
                import_id=import_id,
                tenant_id=tenant_id,
                status='RETRYING',
                error_code=err_type,
                extra={'retry_countdown_seconds': countdown, 'attempt': current_attempt},
                level=logging.WARNING,
            )
            raise self.retry(exc=general_exc, countdown=countdown)
        else:
            dead_letter_msg = f"Exhausted max retries ({current_attempt}/{META_MAX_RETRIES}): {general_exc}"
            logger.exception("Meta lead import %s failed permanently: %s", import_id, dead_letter_msg)
            _record_dead_letter(tenant_id, import_id, current_attempt, 'RETRY_LIMIT_EXCEEDED', dead_letter_msg)
            log_meta_event(
                EVT_DEAD_LETTER,
                dead_letter_msg,
                import_id=import_id,
                tenant_id=tenant_id,
                status='DEAD_LETTER',
                error_code='RETRY_LIMIT_EXCEEDED',
                level=logging.ERROR,
            )
            return {'status': 'DEAD_LETTER', 'error': dead_letter_msg}


def _record_retry_state(tenant_id: str, import_id: str, attempt: int, error_code: str, error_message: str):
    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_meta_leads import MetaLeadImport
            imp = MetaLeadImport.objects.using(db_alias).filter(id=import_id).first()
            if imp and imp.status not in ('IMPORTED', 'RESOLVED'):
                imp.status = 'RETRYING'
                imp.attempt_count = attempt
                imp.error_code = error_code[:50]
                imp.error_message = error_message[:500]
                imp.save(using=db_alias, update_fields=['status', 'attempt_count', 'error_code', 'error_message', 'updated_at'])
    except Exception as exc:
        logger.error("Could not persist RETRYING state for %s: %s", import_id, exc)


def _record_dead_letter(tenant_id: str, import_id: str, attempt: int, error_code: str, error_message: str):
    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_meta_leads import MetaLeadImport
            imp = MetaLeadImport.objects.using(db_alias).filter(id=import_id).first()
            if imp and imp.status not in ('IMPORTED', 'RESOLVED'):
                imp.status = 'DEAD_LETTER'
                imp.attempt_count = attempt
                imp.error_code = error_code[:50]
                imp.error_message = error_message[:500]
                imp.save(using=db_alias, update_fields=['status', 'attempt_count', 'error_code', 'error_message', 'updated_at'])
                record_business_audit(
                    organization=imp.organization,
                    module='crm',
                    action_code='META_IMPORT_DEAD_LETTER',
                    entity_type='MetaLeadImport',
                    entity_id=imp.id,
                    actor_user=None,
                    metadata={'error': error_message[:200], 'retries': attempt},
                    db_alias=db_alias,
                )
    except Exception as exc:
        logger.error("Could not persist DEAD_LETTER state for %s: %s", import_id, exc)


def _record_terminal_failure(tenant_id: str, import_id: str, status_code: str, error_code: str, error_message: str):
    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_meta_leads import MetaLeadImport
            imp = MetaLeadImport.objects.using(db_alias).filter(id=import_id).first()
            if imp and imp.status not in ('IMPORTED', 'RESOLVED'):
                imp.status = status_code
                imp.error_code = error_code[:50]
                imp.error_message = error_message[:500]
                imp.save(using=db_alias, update_fields=['status', 'error_code', 'error_message', 'updated_at'])
    except Exception as exc:
        logger.error("Could not persist terminal failure for %s: %s", import_id, exc)


@shared_task(
    name='apps.tenant_core.tasks_meta_leads.recover_meta_imports_periodic_task'
)
def recover_meta_imports_periodic_task():
    """
    Periodic Celery Beat task scanning all active tenants for stranded PENDING imports
    (e.g. broker disconnection or worker crash during fast-ingest commit) and re-dispatching them.
    """
    from apps.master.models_tenant import Tenant
    from apps.tenant_core.services_meta_recovery import recover_unprocessed_meta_imports

    results = {}
    active_tenants = Tenant.objects.using('default').filter(status='ACTIVE')
    for tenant in active_tenants:
        try:
            res = recover_unprocessed_meta_imports(
                tenant_id=str(tenant.id),
                min_age_seconds=60,
                max_batch_size=50,
                dispatch_async=True,
            )
            if res.get('recovered_count', 0) > 0:
                results[str(tenant.id)] = res
        except Exception as exc:
            logger.warning("Periodic recovery error for tenant %s: %s", tenant.slug, exc)
    return results


# ---------------------------------------------------------------------------
# Scheduled Reconciliation — backfill leads missed during webhook outages
# ---------------------------------------------------------------------------
# Meta webhooks are reliable but not guaranteed. If the webhook URL is wrong,
# the backend is down, or Meta retries are exhausted, leads can be silently
# missed. This task runs every 6 hours and pulls recent leads directly from
# the Graph API for every active Page, creating MetaLeadImport rows for any
# external_lead_id not already present in the database. The UniqueConstraint
# on (organization, mode, external_lead_id) prevents any duplicates.
# ---------------------------------------------------------------------------

_RECONCILE_LOOKBACK_DAYS = int(
    __import__('os').getenv('META_RECONCILE_LOOKBACK_DAYS', '7')
)
_RECONCILE_BATCH_LIMIT = 50  # max leads to pull per page per run


@shared_task(
    name='apps.tenant_core.tasks_meta_leads.reconcile_meta_leads_task',
    bind=True,
    max_retries=0,  # Non-retryable; next scheduled run handles any transient failure
    acks_late=True,
)
def reconcile_meta_leads_task(self):
    """
    Periodic backfill task.

    For every active tenant, iterates over all MetaPageConnection records that:
      - Are active and subscribed to webhooks
      - Have at least one active MetaLeadMapping (i.e., forms are configured)

    For each qualifying page, fetches the most recent N leads from the Graph API
    since the last reconciliation timestamp (or the configured lookback window).
    Any lead not already in MetaLeadImport is created as PENDING and dispatched
    to the standard Celery processing pipeline — identical path to a live webhook.

    Idempotency: the UNIQUE constraint on (organization, mode, external_lead_id)
    ensures duplicate inserts silently fail, never creating double leads.
    """
    from apps.master.models_tenant import Tenant
    from django.utils import timezone as tz
    from datetime import timedelta

    active_tenants = Tenant.objects.using('default').filter(status='ACTIVE')
    overall = {'tenants_scanned': 0, 'pages_scanned': 0, 'leads_backfilled': 0, 'errors': 0}

    client = MetaGraphClient()

    for tenant in active_tenants:
        tenant_id = str(tenant.id)
        overall['tenants_scanned'] += 1

        try:
            with tenant_database_context(tenant_id) as db_alias:
                from apps.tenant_core.models_meta_leads import (
                    MetaPageConnection, MetaLeadMapping, MetaLeadImport
                )
                from apps.tenant_core.meta_crypto import decrypt_token

                # Only process pages that have at least one active mapping (form configured)
                pages_with_mappings = set(
                    MetaLeadMapping.objects.using(db_alias)
                    .filter(is_active=True)
                    .values_list('page_id', flat=True)
                    .distinct()
                )

                active_pages = MetaPageConnection.objects.using(db_alias).filter(
                    is_active=True,
                    is_subscribed_to_webhooks=True,
                    page_id__in=pages_with_mappings,
                ).select_related('organization')

                for page_conn in active_pages:
                    overall['pages_scanned'] += 1
                    page_id = page_conn.page_id
                    org = page_conn.organization

                    page_token = decrypt_token(page_conn.encrypted_page_access_token)
                    if not page_token:
                        logger.warning(
                            "reconcile: tenant=%s page=%s — no page token, skipping",
                            tenant_id, page_id,
                        )
                        continue

                    # Determine lookback window
                    if page_conn.last_reconcile_attempted_at:
                        # Slight overlap of 10 minutes to cover clock skew / in-flight events
                        since_dt = page_conn.last_reconcile_attempted_at - timedelta(minutes=10)
                    else:
                        since_dt = tz.now() - timedelta(days=_RECONCILE_LOOKBACK_DAYS)

                    since_ts = int(since_dt.timestamp())
                    until_ts = int(tz.now().timestamp())

                    try:
                        leads_backfilled = _reconcile_page_leads(
                            client=client,
                            page_id=page_id,
                            page_token=page_token,
                            org=org,
                            db_alias=db_alias,
                            tenant_id=tenant_id,
                            since_ts=since_ts,
                            until_ts=until_ts,
                        )
                        overall['leads_backfilled'] += leads_backfilled

                        # Stamp reconcile health fields
                        MetaPageConnection.objects.using(db_alias).filter(
                            pk=page_conn.pk
                        ).update(
                            last_reconcile_attempted_at=tz.now(),
                            last_reconcile_leads_found=leads_backfilled,
                        )

                    except (MetaTokenExpiredError, MetaPermissionError) as perm_err:
                        logger.warning(
                            "reconcile: tenant=%s page=%s — permanent Meta error: %s",
                            tenant_id, page_id, perm_err,
                        )
                        overall['errors'] += 1
                    except (MetaRateLimitError, MetaGraphServerError, requests.exceptions.RequestException) as trans_err:
                        logger.warning(
                            "reconcile: tenant=%s page=%s — transient error (will retry next run): %s",
                            tenant_id, page_id, trans_err,
                        )
                        overall['errors'] += 1
                    except Exception as exc:
                        logger.exception(
                            "reconcile: tenant=%s page=%s — unexpected error: %s",
                            tenant_id, page_id, exc,
                        )
                        overall['errors'] += 1

        except Exception as tenant_exc:
            logger.exception("reconcile: tenant=%s — failed to process: %s", tenant_id, tenant_exc)
            overall['errors'] += 1

    logger.info("reconcile_meta_leads_task completed: %s", overall)
    return overall


def _reconcile_page_leads(
    client: 'MetaGraphClient',
    page_id: str,
    page_token: str,
    org,
    db_alias: str,
    tenant_id: str,
    since_ts: int,
    until_ts: int,
) -> int:
    """
    Fetch leads for a single page from Meta Graph API between since_ts and until_ts.
    For each lead not already in MetaLeadImport, create a PENDING record and dispatch
    to the standard processing pipeline.

    Returns the count of newly backfilled leads.
    """
    from apps.tenant_core.models_meta_leads import MetaLeadMapping, MetaLeadImport
    import hashlib
    import json

    # Pull all active form IDs for this page so we know which forms to query
    active_form_ids = list(
        MetaLeadMapping.objects.using(db_alias)
        .filter(organization=org, page_id=page_id, is_active=True)
        .values_list('form_id', flat=True)
        .distinct()
    )

    if not active_form_ids:
        return 0

    # Fetch leads per form from Meta Graph API
    # GET /{form_id}/leads?since=X&until=Y&fields=...&limit=N
    from apps.tenant_core.services_meta_graph import GRAPH_API_BASE
    import requests as _requests

    backfilled = 0

    for form_id in active_form_ids:
        url = f"{GRAPH_API_BASE}/{form_id}/leads"
        params = {
            'access_token': page_token,
            'fields': 'id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,is_organic',
            'since': since_ts,
            'until': until_ts,
            'limit': _RECONCILE_BATCH_LIMIT,
        }

        try:
            resp = _requests.get(url, params=params, timeout=30)
            data = client._handle_response(resp)
        except (MetaTokenExpiredError, MetaPermissionError):
            raise  # Propagate to caller for proper error classification
        except Exception:
            raise

        leads = data.get('data', [])

        for lead in leads:
            external_lead_id = str(lead.get('id', ''))
            if not external_lead_id:
                continue

            field_data = lead.get('field_data', [])
            payload_hash = hashlib.sha256(
                json.dumps(field_data, sort_keys=True).encode()
            ).hexdigest()

            # Check if already imported (idempotency)
            already_exists = MetaLeadImport.objects.using(db_alias).filter(
                organization=org,
                external_lead_id=external_lead_id,
            ).exists()

            if already_exists:
                continue

            # Create as PENDING — standard processing pipeline will handle the rest
            try:
                with transaction.atomic(using=db_alias):
                    imp = MetaLeadImport.objects.using(db_alias).create(
                        organization=org,
                        mode='LIVE',
                        page_id=page_id,
                        form_id=form_id,
                        external_lead_id=external_lead_id,
                        payload_hash=payload_hash,
                        field_data=field_data,
                        campaign_id=str(lead.get('campaign_id') or ''),
                        campaign_name=str(lead.get('campaign_name') or ''),
                        adset_id=str(lead.get('adset_id') or ''),
                        adset_name=str(lead.get('adset_name') or ''),
                        ad_id=str(lead.get('ad_id') or ''),
                        ad_name=str(lead.get('ad_name') or ''),
                        is_organic=bool(lead.get('is_organic', False)),
                        status='PENDING',
                    )

                # Dispatch to Celery worker (same path as a live webhook)
                process_meta_lead_import_task.delay(
                    tenant_id=tenant_id,
                    import_id=str(imp.id),
                )
                backfilled += 1

            except Exception as create_err:
                # UniqueConstraint violation means it was already created concurrently — safe to ignore
                if 'unique' in str(create_err).lower() or 'duplicate' in str(create_err).lower():
                    continue
                logger.warning(
                    "reconcile: could not create import for lead %s page=%s form=%s: %s",
                    external_lead_id, page_id, form_id, create_err,
                )

    return backfilled
