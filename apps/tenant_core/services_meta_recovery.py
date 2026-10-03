"""
apps/tenant_core/services_meta_recovery.py — Durable recovery service for Meta Lead Ads imports.

Recovers any live leadgen imports that were committed to PostgreSQL with status='PENDING'
but were never published or consumed by the Celery worker queue (e.g. broker blips,
worker crash during publish, or network failure).
"""

import logging
from datetime import timedelta
from typing import List, Dict, Any, Optional

from django.db import transaction
from django.utils import timezone

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.models_meta_leads import MetaLeadImport
from apps.tenant_core.services_meta_leads import process_live_import
from apps.tenant_core.services_reliability import record_business_audit

logger = logging.getLogger(__name__)


def recover_unprocessed_meta_imports(
    tenant_id: str,
    min_age_seconds: int = 60,
    max_batch_size: int = 50,
    dispatch_async: bool = False,
    alias: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Scans the tenant database for stuck PENDING imports and processes or re-enqueues them.
    Uses select_for_update(skip_locked=True) to prevent concurrent worker collisions.
    """
    if not alias:
        tenant = Tenant.objects.using('default').get(id=tenant_id)
        with tenant_database_context(tenant.id) as db_alias:
            return _execute_recovery(tenant_id, db_alias, min_age_seconds, max_batch_size, dispatch_async)
    else:
        return _execute_recovery(tenant_id, alias, min_age_seconds, max_batch_size, dispatch_async)


def _execute_recovery(
    tenant_id: str,
    alias: str,
    min_age_seconds: int,
    max_batch_size: int,
    dispatch_async: bool,
) -> Dict[str, Any]:
    cutoff = timezone.now() - timedelta(seconds=min_age_seconds)

    recovered_ids: List[str] = []
    failed_ids: List[str] = []

    with transaction.atomic(using=alias):
        stuck_imports = list(
            MetaLeadImport.objects.using(alias)
            .select_for_update(skip_locked=True)
            .filter(
                mode='LIVE',
                status='PENDING',
                received_at__lte=cutoff,
            )
            .order_by('received_at')[:max_batch_size]
        )

        for imp in stuck_imports:
            imp_id = str(imp.id)
            try:
                if dispatch_async:
                    from apps.tenant_core.tasks_meta_leads import process_meta_lead_import_task
                    process_meta_lead_import_task.delay(str(tenant_id), imp_id)
                    recovered_ids.append(imp_id)
                else:
                    processed = process_live_import(imp.organization, imp.id, alias=alias)
                    if processed.status in ('IMPORTED', 'NEEDS_REVIEW', 'NEEDS_MAPPING', 'NEEDS_ASSIGNMENT'):
                        recovered_ids.append(imp_id)
                    else:
                        failed_ids.append(imp_id)

                record_business_audit(
                    organization=imp.organization,
                    module='crm',
                    action_code='META_IMPORT_RECOVERED',
                    entity_type='MetaLeadImport',
                    entity_id=imp.id,
                    actor_user=None,
                    metadata={'mode': 'LIVE', 'import_id': imp_id, 'async': dispatch_async},
                    db_alias=alias,
                )
            except Exception as exc:
                logger.error("Failed to recover Meta import %s: %s", imp_id, exc)
                failed_ids.append(imp_id)

    logger.info(
        "Meta Lead recovery completed for tenant %s: recovered=%d failed=%d",
        tenant_id,
        len(recovered_ids),
        len(failed_ids),
    )

    return {
        'tenant_id': str(tenant_id),
        'recovered_count': len(recovered_ids),
        'failed_count': len(failed_ids),
        'recovered_ids': recovered_ids,
        'failed_ids': failed_ids,
    }
