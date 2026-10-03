"""
apps/tenant_core/tasks_meta_leads.py — Asynchronous Celery Tasks for Meta Lead Ads Ingestion.

Ensures:
- Fast HTTP 200 return to Meta Webhook gateway by offloading Graph API retrieval to worker
- Bounded exponential retries for network timeouts and Meta rate limits
- Permanent failure classification (token revoked, lead not found) with zero retries
- Complete tenant database isolation via tenant_database_context
"""
import logging
from celery import shared_task
from django.db import transaction

from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_org import Organization
from apps.tenant_core.services_meta_leads import process_live_import
from apps.tenant_core.services_meta_graph import MetaRateLimitError, MetaTokenExpiredError, MetaLeadNotFoundError

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_meta_leads.process_meta_lead_import_task',
    max_retries=5,
    default_retry_delay=15,
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

    logger.info("Executing Meta lead import task: tenant=%s import_id=%s attempt=%d", tenant_id, import_id, self.request.retries + 1)

    try:
        with tenant_database_context(tenant_id) as db_alias:
            from apps.tenant_core.models_meta_leads import MetaLeadImport
            imp = MetaLeadImport.objects.using(db_alias).select_related('organization').filter(id=import_id).first()
            if not imp:
                logger.warning("MetaLeadImport %s not found in tenant DB %s", import_id, db_alias)
                return {'status': 'FAILED', 'error': 'Import record not found'}

            event = process_live_import(organization=imp.organization, event_id=import_id, actor_user=None, alias=db_alias)
            logger.info("Meta lead import processed: import_id=%s status=%s lead_id=%s", import_id, event.status, event.lead_id)
            return {'status': event.status, 'lead_id': str(event.lead_id) if event.lead_id else None}

    except MetaRateLimitError as exc:
        countdown = min(300, (2 ** self.request.retries) * 10)
        logger.warning("Meta rate limit encountered for import %s. Retrying in %ds... (attempt %d/5)", import_id, countdown, self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown)

    except MetaTokenExpiredError as exc:
        logger.error("Meta token expired for import %s. Stopping task; re-auth required: %s", import_id, exc)
        return {'status': 'FAILED', 'error': 'TOKEN_EXPIRED'}

    except MetaLeadNotFoundError as exc:
        logger.error("Meta lead not found on Facebook servers for import %s: %s", import_id, exc)
        return {'status': 'FAILED', 'error': 'LEAD_NOT_FOUND'}

    except Exception as exc:
        # Transient connection / server errors
        if self.request.retries < self.max_retries:
            countdown = min(120, (2 ** self.request.retries) * 5)
            logger.warning("Transient error processing Meta lead %s (%s). Retrying in %ds...", import_id, exc, countdown)
            raise self.retry(exc=exc, countdown=countdown)
        else:
            logger.exception("Max retries exceeded for Meta lead import %s: %s", import_id, exc)
            err_msg = f"Exhausted max retries (5/5): {exc}"
            try:
                with tenant_database_context(tenant_id) as db_alias:
                    from apps.tenant_core.models_meta_leads import MetaLeadImport
                    from apps.tenant_core.services_reliability import record_business_audit
                    imp = MetaLeadImport.objects.using(db_alias).filter(id=import_id).first()
                    if imp:
                        imp.status = 'FAILED'
                        imp.error_code = 'MAX_RETRIES_EXCEEDED'
                        imp.error_message = err_msg
                        imp.save(using=db_alias, update_fields=['status', 'error_code', 'error_message', 'updated_at'])
                        record_business_audit(
                            organization=imp.organization,
                            module='crm',
                            action_code='META_IMPORT_EXHAUSTED',
                            entity_type='MetaLeadImport',
                            entity_id=imp.id,
                            actor_user=None,
                            metadata={'error': str(exc), 'retries': 5},
                            db_alias=db_alias,
                        )
            except Exception as db_err:
                logger.error("Could not persist exhausted state for %s: %s", import_id, db_err)
            return {'status': 'FAILED', 'error': err_msg}


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
