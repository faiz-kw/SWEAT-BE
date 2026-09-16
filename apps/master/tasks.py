"""
apps/master/tasks.py — Asynchronous Celery Tasks for Platform & Tenant Operations.

Provides:
- provision_tenant_async: Background task executing 14-step tenant onboarding.
- migrate_tenant_async: Background task running tenant schema migrations in isolated context.
- migrate_all_tenants_async: Orchestrator fanning out per-tenant migrations asynchronously.
"""

import sys
import uuid
import logging
from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError, OperationalError

from apps.tenant_core.context import tenant_database_context
from apps.tenant_core.locks import (
    redis_distributed_lock,
    redis_concurrency_semaphore,
    LockAcquisitionError,
    ConcurrencyLimitExceeded,
)
from apps.master.provisioning import TenantProvisioningEngine, ProvisioningError
from apps.master.models_infra import TenantProvisioning, PlatformAuditEvent
from config.routers import TenantRouter, build_tenant_db_alias

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.master.tasks.provision_tenant_async',
    autoretry_for=(OperationalError, DatabaseError),
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def provision_tenant_async(self, provisioning_id: str, payload: dict) -> dict:
    """
    Asynchronously executes the full 14-step TenantProvisioningEngine pipeline.

    Security & Reliability:
    - Master DB isolation: All operational records stored in Master DB ('default').
    - Lock Lease Safety: Acquires Redis distributed lock with auto_renew=True
      (heartbeat thread renews TTL while task is legitimately running; process crash
      kills thread and allows TTL expiration).
    - Captures and stores Celery task ID in TenantProvisioning.celery_task_id.
    - Emits Sprint 6-compliant PlatformAuditEvent under SYSTEM_JOB actor context.
    - Idempotent: Exits cleanly if record already COMPLETED.
    """
    logger.info("Starting async provisioning task for ID: %s", provisioning_id)

    provisioning = TenantProvisioning.objects.using('default').filter(id=provisioning_id).first()
    if not provisioning:
        logger.error("TenantProvisioning record '%s' not found on Master DB.", provisioning_id)
        raise ValueError(f"TenantProvisioning record '{provisioning_id}' does not exist.")

    # Ensure celery_task_id is recorded
    if not provisioning.celery_task_id:
        provisioning.celery_task_id = self.request.id or ''
        provisioning.save(using='default', update_fields=['celery_task_id', 'updated_at'])

    # Fast-exit if already completed
    if provisioning.status == 'COMPLETED':
        logger.info("Provisioning record '%s' is already COMPLETED. Exiting task.", provisioning_id)
        return {
            'status': 'COMPLETED',
            'provisioning_id': str(provisioning.id),
            'tenant_id': str(provisioning.tenant_id) if provisioning.tenant_id else None,
            'message': 'Tenant already provisioned successfully.',
        }

    # Derive locking key based on tenant slug/brand_name
    brand_name = payload.get('brand_name', '')
    slug = payload.get('slug') or brand_name.lower().replace(' ', '-')
    lock_key = f"lock:tenant_provisioning:{slug}"

    try:
        # Lock lease renewal: heartbeat renews TTL every timeout/3 seconds while running
        with redis_distributed_lock(lock_key, timeout_seconds=600, blocking=False, auto_renew=True):
            engine = TenantProvisioningEngine(initiated_by=provisioning.initiated_by)
            result = engine.provision(payload, provisioning=provisioning)

            # Record Platform Audit Event (Sprint 6 audit compliance via SYSTEM_JOB context)
            try:
                PlatformAuditEvent.objects.using('default').create(
                    actor=provisioning.initiated_by,
                    actor_email=provisioning.initiated_by.email if provisioning.initiated_by else 'system@internal',
                    action='PROVISION_TENANT',
                    resource_type='Tenant',
                    resource_id=result.get('tenant_id', ''),
                    description=f"Asynchronously provisioned tenant '{slug}' via Celery task {self.request.id}.",
                    tenant_context=provisioning.tenant,
                )
            except Exception as audit_exc:
                logger.warning("Failed to record platform audit event for tenant '%s': %s", slug, audit_exc)

            return result

    except LockAcquisitionError as lock_err:
        logger.warning(
            "Lock collision for tenant '%s': %s. Task will not re-execute concurrently.",
            slug, lock_err
        )
        return {
            'status': 'LOCKED',
            'provisioning_id': str(provisioning.id),
            'error': str(lock_err),
        }
    except ProvisioningError as prov_err:
        logger.error("Provisioning failed for record '%s': %s", provisioning_id, prov_err)
        # Bounded retry only if transient database connection issue
        if 'connection' in str(prov_err).lower() and self.request.retries < self.max_retries:
            raise self.retry(exc=prov_err)
        return {
            'status': 'FAILED',
            'provisioning_id': str(provisioning.id),
            'error': str(prov_err),
        }
    except Exception as exc:
        logger.exception("Unexpected error in provisioning task '%s': %s", provisioning_id, exc)
        provisioning.status = 'FAILED'
        provisioning.error_message = str(exc)
        provisioning.save(using='default', update_fields=['status', 'error_message', 'updated_at'])
        return {
            'status': 'FAILED',
            'provisioning_id': str(provisioning.id),
            'error': str(exc),
        }


@shared_task(
    bind=True,
    name='apps.master.tasks.migrate_tenant_async',
    max_retries=2,
    default_retry_delay=5,
    acks_late=True,
    reject_on_worker_lost=True,
)
def migrate_tenant_async(self, tenant_id: str) -> dict:
    """
    Asynchronously executes tenant_core schema migrations for a single tenant database.

    Security & Context Properties:
    - Input: Strictly trusted tenant UUID string. Never accepts arbitrary DB aliases or credentials.
    - Resolves Tenant and TenantDataSource strictly from Master DB ('default').
    - Concurrency Bounding: Acquires distributed concurrency semaphore slot ('semaphore:tenant_migrations').
    - Per-Tenant Isolation: Acquires per-tenant Redis distributed lock with lease auto-renewal.
    - Establishes tenant_database_context(tenant_id) to configure dynamic alias and thread-local routing.
    - Strictly asserts Master DB ('default') is NEVER targeted for tenant_core migrations.
    - Guarantees complete connection cleanup in finally block via context manager.
    """
    from apps.master.models_tenant import Tenant
    from apps.master.models_infra import TenantDataSource

    logger.info("Starting async migration for tenant: %s", tenant_id)

    # Validate UUID format
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid tenant UUID '{tenant_id}'.") from exc

    # Master DB resolution
    tenant = Tenant.objects.using('default').filter(id=tenant_uuid).first()
    if not tenant:
        raise ValueError(f"Tenant '{tenant_uuid}' does not exist on Master DB.")

    if tenant.status != 'ACTIVE':
        logger.warning("Tenant '%s' is not ACTIVE (status=%s). Skipping migration.", tenant.slug, tenant.status)
        return {
            'status': 'SKIPPED',
            'tenant_id': str(tenant.id),
            'tenant_slug': tenant.slug,
            'reason': f"Tenant status is {tenant.status}, expected ACTIVE.",
        }

    data_source = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
    if not data_source or not data_source.db_name:
        raise ValueError(f"No active TenantDataSource configured for tenant '{tenant.slug}'.")

    concurrency_limit = getattr(settings, 'TENANT_MIGRATION_CONCURRENCY_LIMIT', 4)
    sem_key = "semaphore:tenant_migrations"
    task_token = self.request.id or str(uuid.uuid4())
    lock_key = f"lock:tenant_migration:{tenant_id}"

    try:
        # 1. Global Concurrency Limiter: bounded aggregate parallelism across all workers
        with redis_concurrency_semaphore(sem_key, max_concurrent=concurrency_limit, timeout_seconds=300, identifier=task_token):
            # 2. Per-Tenant Distributed Lock: prevents simultaneous migrations for the same tenant
            with redis_distributed_lock(lock_key, timeout_seconds=300, blocking=False, auto_renew=True):
                # 3. Dynamic tenant database context
                with tenant_database_context(tenant_id) as db_alias:
                    # Master DB protection check
                    if db_alias == 'default' or not (db_alias.startswith('tenant_') or db_alias == 'tenant_test'):
                        raise ValueError(f"Unsafe database alias '{db_alias}' targeting Master DB. Aborting.")

                    router = TenantRouter()
                    if not router.allow_migrate(db_alias, 'tenant_core'):
                        raise ValueError(f"TenantRouter disallows tenant_core migration for alias '{db_alias}'.")

                    logger.info("Executing tenant_core migration against alias '%s' for tenant '%s'...", db_alias, tenant.slug)
                    call_command(
                        'migrate',
                        'tenant_core',
                        database=db_alias,
                        interactive=False,
                        verbosity=1,
                    )

                    # Update schema version metadata on Master DB
                    data_source.db_schema_version = '1.0'
                    data_source.save(using='default', update_fields=['db_schema_version', 'updated_at'])

                    logger.info("Successfully completed migration for tenant '%s'.", tenant.slug)
                    return {
                        'status': 'SUCCESS',
                        'tenant_id': str(tenant.id),
                        'tenant_slug': tenant.slug,
                        'alias': db_alias,
                        'db_name': data_source.db_name,
                    }

    except ConcurrencyLimitExceeded as sem_err:
        logger.info(
            "Tenant migration concurrency limit (%d) reached. Retrying migration for tenant '%s'...",
            concurrency_limit, tenant.slug
        )
        raise self.retry(exc=sem_err, countdown=5)
    except LockAcquisitionError as lock_err:
        logger.warning("Migration lock collision for tenant '%s': %s", tenant.slug, lock_err)
        return {
            'status': 'LOCKED',
            'tenant_id': str(tenant.id),
            'tenant_slug': tenant.slug,
            'error': str(lock_err),
        }
    except Exception as exc:
        logger.exception("Migration failed for tenant '%s': %s", tenant.slug, exc)
        return {
            'status': 'FAILED',
            'tenant_id': str(tenant.id),
            'tenant_slug': tenant.slug,
            'error': str(exc),
        }


@shared_task(
    bind=True,
    name='apps.master.tasks.migrate_all_tenants_async',
    acks_late=True,
    reject_on_worker_lost=True,
)
def migrate_all_tenants_async(self, batch_size: int = None) -> dict:
    """
    Orchestration task that fans out tenant migrations across all active tenants.

    Controlled Parallelism:
    - Resolves all ACTIVE tenants with active data sources from Master DB.
    - Dispatches individual `migrate_tenant_async` tasks with staggered countdowns
      (based on TENANT_MIGRATION_CONCURRENCY_LIMIT) to prevent message queue spikes.
    - Each task coordinates through `redis_concurrency_semaphore` to strictly enforce
      aggregate migration concurrency limits across multiple Celery worker nodes.
    - Per-tenant Redis locks ensure no overlapping execution for the same tenant.
    - Returns summary of dispatched task IDs.
    """
    from apps.master.models_tenant import Tenant
    from apps.master.models_infra import TenantDataSource

    concurrency_limit = batch_size or getattr(settings, 'TENANT_MIGRATION_CONCURRENCY_LIMIT', 4)
    active_tenants = Tenant.objects.using('default').filter(status='ACTIVE')
    dispatched = []
    skipped = []

    for idx, tenant in enumerate(active_tenants):
        ds = TenantDataSource.objects.using('default').filter(tenant=tenant, status='ACTIVE').first()
        if not ds or not ds.db_name:
            skipped.append({'tenant_id': str(tenant.id), 'tenant_slug': tenant.slug, 'reason': 'No active DataSource'})
            continue

        # Controlled scheduling: stagger dispatch by 2s per batch chunk to prevent task storms
        stagger_delay = (len(dispatched) // concurrency_limit) * 2
        task = migrate_tenant_async.apply_async(args=[str(tenant.id)], countdown=stagger_delay)
        dispatched.append({
            'tenant_id': str(tenant.id),
            'tenant_slug': tenant.slug,
            'celery_task_id': task.id,
        })

    logger.info(
        "migrate_all_tenants_async dispatched %d tasks (%d skipped) with concurrency limit %d.",
        len(dispatched), len(skipped), concurrency_limit
    )
    return {
        'total_active_tenants': active_tenants.count(),
        'dispatched_count': len(dispatched),
        'skipped_count': len(skipped),
        'dispatched_tasks': dispatched,
        'skipped_tenants': skipped,
        'concurrency_limit': concurrency_limit,
    }


# ============================================================================
# SPRINT 10 COMMERCIAL BILLING BACKGROUND TASKS
# ============================================================================

@shared_task(
    bind=True,
    name='apps.master.tasks.process_billing_webhook_async',
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def process_billing_webhook_async(self, event_id: str):
    """
    Processes an ingested billing webhook event asynchronously.
    """
    from apps.master.billing.services import WebhookProcessingService
    logger.info("Starting async webhook processing for event: %s", event_id)
    try:
        WebhookProcessingService.process_event(event_id)
        return {'status': 'SUCCESS', 'event_id': event_id}
    except Exception as exc:
        logger.exception("Error processing webhook event %s: %s", event_id, exc)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    name='apps.master.tasks.execute_dunning_retry_async',
    max_retries=2,
    default_retry_delay=300,
    acks_late=True,
)
def execute_dunning_retry_async(self, dunning_event_id: str):
    """
    Executes a scheduled dunning payment retry.
    """
    from apps.master.billing.services import DunningService
    logger.info("Executing dunning retry for event: %s", dunning_event_id)
    try:
        success = DunningService.execute_retry(dunning_event_id)
        return {'status': 'SUCCESS' if success else 'RETRY_FAILED', 'dunning_event_id': dunning_event_id}
    except Exception as exc:
        logger.exception("Error executing dunning retry for event %s: %s", dunning_event_id, exc)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    name='apps.master.tasks.renew_due_subscriptions_async',
    acks_late=True,
)
def renew_due_subscriptions_async(self):
    """
    Scheduled job: finds active subscriptions due for renewal and triggers renewal & invoice generation.
    """
    from django.utils import timezone
    from apps.master.models_saas import TenantSubscription
    from apps.master.billing.services import SubscriptionLifecycleService
    now = timezone.now()
    due_subs = TenantSubscription.objects.using('default').filter(
        status='ACTIVE',
        next_renewal_at__lte=now,
    )
    results = []
    for sub in due_subs:
        try:
            inv, _ = SubscriptionLifecycleService.renew_subscription(sub)
            results.append({'subscription_id': str(sub.id), 'invoice_id': str(inv.id), 'status': 'RENEWED'})
        except Exception as exc:
            logger.exception("Error renewing subscription %s: %s", sub.id, exc)
            results.append({'subscription_id': str(sub.id), 'status': 'ERROR', 'error': str(exc)})

    return {'renewed_count': len(results), 'results': results}


@shared_task(
    bind=True,
    name='apps.master.tasks.sync_all_tenant_resource_usage_async',
    acks_late=True,
)
def sync_all_tenant_resource_usage_async(self) -> dict:
    """
    Scheduled operational task: aggregates active resource usage across all active tenants.
    Emits SYSTEM_JOB PlatformAuditEvent.
    """
    import time
    from django.utils import timezone
    from apps.master.metering import sync_tenant_resource_usage
    from apps.master.models_tenant import Tenant
    from apps.master.models_infra import PlatformAuditEvent

    logger.info("Starting scheduled resource usage sync across all active tenants.")
    start_time = time.time()
    results = sync_tenant_resource_usage()
    duration = time.time() - start_time

    # Record Platform Audit Event under SYSTEM_JOB actor context
    try:
        PlatformAuditEvent.objects.using('default').create(
            actor_email='system@scheduler',
            action='RESOURCE_USAGE_SYNCED',
            resource_type='TenantResourceUsage',
            resource_id=None,
            description=(
                f"Periodic resource usage aggregated: {results.get('succeeded', 0)} succeeded, "
                f"{results.get('failed', 0)} failed in {duration:.2f}s."
            ),
        )
    except Exception as audit_err:
        logger.warning("Failed to emit audit event for resource usage sync: %s", audit_err)

    return results


@shared_task(
    bind=True,
    name='apps.master.tasks.check_all_tenant_databases_health_async',
    acks_late=True,
)
def check_all_tenant_databases_health_async(self) -> dict:
    """
    Scheduled operational task: evaluates connection health and latency for all tenant data sources.
    Records TenantDataSourceHealth records on Master DB.
    """
    import time
    from django.utils import timezone
    from django.db import connections
    from apps.master.models_infra import TenantDataSource, TenantDataSourceHealth
    from config.tenant_middleware import _register_tenant_connection, unregister_tenant_connection

    data_sources = TenantDataSource.objects.using('default').filter(status='ACTIVE').select_related('tenant')
    checked = 0
    healthy = 0
    degraded = 0

    for ds in data_sources:
        checked += 1
        db_name = ds.database_name or ds.db_name
        if not db_name:
            continue

        if 'test' in getattr(sys, 'argv', []) and 'tenant_test' in settings.DATABASES and (db_name in ('fitness_tenant', 'test_fitness_tenant', 'test')):
            alias = 'tenant_test'
        else:
            alias = build_tenant_db_alias(ds.tenant_id)

        t0 = time.time()
        try:
            _register_tenant_connection(alias, db_name, data_source=ds, tenant_id=ds.tenant_id, force_refresh=False)
            conn = connections[alias]
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1;")
                cursor.fetchone()
            latency_ms = max(1, int((time.time() - t0) * 1000))
            status_val = 'HEALTHY' if latency_ms < 500 else 'DEGRADED'
            err_msg = ''
            if status_val == 'HEALTHY':
                healthy += 1
            else:
                degraded += 1
        except Exception as exc:
            latency_ms = max(1, int((time.time() - t0) * 1000))
            status_val = 'UNREACHABLE'
            err_msg = str(exc)[:500]
            logger.warning("Database health check failed for tenant %s: %s", ds.tenant.slug, exc)
        finally:
            if alias != 'tenant_test':
                try:
                    if alias in connections:
                        connections[alias].close()
                except Exception:
                    pass
                unregister_tenant_connection(alias)

        # Record health check record on Master DB
        TenantDataSourceHealth.objects.using('default').create(
            tenant=ds.tenant,
            data_source=ds,
            status=status_val,
            response_time_ms=latency_ms,
            error_message=err_msg,
            checked_at=timezone.now(),
        )

        # Update last_health_check_at on DataSource
        ds.last_health_check_at = timezone.now()
        ds.save(using='default', update_fields=['last_health_check_at', 'updated_at'])

    return {
        'total_checked': checked,
        'healthy': healthy,
        'degraded': degraded,
    }


@shared_task(
    bind=True,
    name='apps.master.tasks.sync_tenant_catalogs_async',
    acks_late=True,
)
def sync_tenant_catalogs_async(self) -> dict:
    """
    Scheduled operational task: synchronizes module and submodule catalogs into all active tenant databases.
    """
    from django.core.management import call_command
    from apps.master.models_infra import PlatformAuditEvent

    logger.info("Starting scheduled catalog synchronization across tenant databases.")
    try:
        call_command('sync_tenant_catalog', all=True)
        PlatformAuditEvent.objects.using('default').create(
            actor_email='system@scheduler',
            action='TENANT_CATALOGS_SYNCED',
            resource_type='ModuleCatalog',
            resource_id=None,
            description="Periodic tenant catalog synchronization completed successfully.",
        )
        return {'status': 'SUCCESS', 'message': 'Tenant catalogs synchronized.'}
    except Exception as exc:
        logger.exception("Error during scheduled tenant catalog synchronization: %s", exc)
        return {'status': 'FAILED', 'error': str(exc)}

