"""
apps/tenant_core/tasks.py — Asynchronous Celery Tasks for Tenant DSR & Privacy Operations.

Provides:
- process_dsr_export_task: Compiles personal data, generates export package, saves file metadata, completes request.
- process_dsr_erasure_task: Irreversibly pseudonymizes/anonymizes user PII, deactivates user, preserves immutable audit log.
"""

import uuid
import json
import logging
from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from django.core.serializers.json import DjangoJSONEncoder

from .models_privacy import PrivacyRequest, ConsentRecord, TenantAuditEvent
from .models_users import TenantUser
from .models_infra import File
from .audit import emit_audit_event
from apps.tenant_core.context import tenant_database_context
from config.routers import TenantRoutingError

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_dsr_export_task',
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,
)
def process_dsr_export_task(self, tenant_id: str = None, privacy_request_id: str = None, **kwargs) -> dict:
    """
    Executes DSR Right of Access / Data Portability export workflow asynchronously.
    Requires trusted tenant_id and privacy_request_id.
    Strictly establishes tenant_database_context and fails closed if missing or invalid.
    """
    if not tenant_id or not privacy_request_id:
        logger.error("DSR export task failed closed: tenant_id and privacy_request_id are mandatory.")
        return {'status': 'FAILED', 'error': 'tenant_id and privacy_request_id are required'}

    if kwargs.get('db_alias') == 'default':
        logger.error("DSR export task rejected: Tenant operations must never execute against Master DB ('default').")
        return {'status': 'FAILED', 'error': "Tenant tasks cannot execute on 'default' database."}

    from .context import tenant_database_context
    from config.routers import TenantRoutingError

    try:
        with tenant_database_context(tenant_id) as db_alias:
            logger.info("Starting DSR export task for tenant '%s', request '%s' on alias '%s'", tenant_id, privacy_request_id, db_alias)

            try:
                req = PrivacyRequest.objects.using(db_alias).select_related('user').get(id=privacy_request_id)
            except PrivacyRequest.DoesNotExist:
                logger.error("PrivacyRequest '%s' not found on DB alias '%s' for tenant '%s'", privacy_request_id, db_alias, tenant_id)
                return {'status': 'FAILED', 'error': 'PrivacyRequest not found'}

            req.status = 'IN_PROGRESS'
            req.save(using=db_alias, update_fields=['status', 'updated_at'])

            user = req.user

            # 1. Compile all personal data categories from tenant database
            user_data = {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone,
                'status': user.status,
                'is_login_allowed': user.is_login_allowed,
                'created_at': user.created_at.isoformat() if user.created_at else None,
                'home_branch_id': str(user.home_branch_id) if user.home_branch_id else None,
            }

            consents = list(
                ConsentRecord.objects.using(db_alias).filter(user=user).values(
                    'id', 'purpose__code', 'purpose__name', 'status', 'notice_version',
                    'granted_at', 'withdrawn_at', 'capture_source', 'created_at'
                )
            )

            audit_logs = list(
                TenantAuditEvent.objects.using(db_alias).filter(actor=user).values(
                    'id', 'action', 'resource_type', 'resource_id', 'created_at'
                )[:200]
            )

            export_payload = {
                'export_metadata': {
                    'privacy_request_id': str(req.id),
                    'request_type': req.request_type,
                    'generated_at': timezone.now().isoformat(),
                    'user_id': str(user.id),
                },
                'user_profile': user_data,
                'consent_records': consents,
                'audit_activity_summary': audit_logs,
            }

            payload_json = json.dumps(export_payload, cls=DjangoJSONEncoder, indent=2)
            payload_bytes = payload_json.encode('utf-8')

            # 2. Store export file record in File table
            file_id = uuid.uuid4()
            object_key = f"tenants/privacy_exports/{user.id}/{req.id}.json"

            file_obj = File.objects.using(db_alias).create(
                id=file_id,
                owner_type='TenantUser',
                owner_id=user.id,
                file_name=f"privacy_export_{user.id}_{req.id}.json",
                original_file_name=f"privacy_export_{user.id}.json",
                storage_provider='ZATA_S3',
                bucket_reference='zata-private-storage',
                object_key=object_key,
                mime_type='application/json',
                file_size=len(payload_bytes),
                checksum=str(hash(payload_bytes)),
                classification='CONFIDENTIAL',
                uploaded_by=user,
            )

            # 3. Update PrivacyRequest to COMPLETED with resolution and evidence
            now = timezone.now()
            req.status = 'COMPLETED'
            req.completed_at = now
            req.resolution = 'Personal data export package generated successfully.'
            req.evidence = {
                'file_id': str(file_obj.id),
                'object_key': object_key,
                'file_size_bytes': len(payload_bytes),
                'consents_count': len(consents),
                'audit_events_count': len(audit_logs),
            }
            req.save(using=db_alias, update_fields=['status', 'completed_at', 'resolution', 'evidence', 'updated_at'])

            # 4. Emit auditable event
            try:
                emit_audit_event(
                    action='DSR_EXPORT_COMPLETED',
                    resource_type='PrivacyRequest',
                    resource_id=req.id,
                    actor_type='SYSTEM_JOB',
                    after_state={'status': 'COMPLETED', 'file_id': str(file_obj.id)},
                    description=f"DSR {req.request_type} export completed asynchronously for user {user.email}.",
                    db_alias=db_alias,
                )
            except Exception as e:
                logger.warning("Failed to emit audit event for DSR export: %s", e)

            logger.info("Successfully completed DSR export for request '%s'", req.id)
            return {
                'status': 'COMPLETED',
                'privacy_request_id': str(req.id),
                'file_id': str(file_obj.id),
            }
    except TenantRoutingError as tre:
        logger.error("DSR export task failed to resolve tenant database context for tenant '%s': %s", tenant_id, tre)
        return {'status': 'FAILED', 'error': str(tre)}


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_dsr_erasure_task',
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,
)
def process_dsr_erasure_task(self, tenant_id: str = None, privacy_request_id: str = None, **kwargs) -> dict:
    """
    Executes DSR Right to Erasure / Deletion workflow asynchronously.
    Requires trusted tenant_id and privacy_request_id.
    Strictly establishes tenant_database_context and fails closed if missing or invalid.
    """
    if not tenant_id or not privacy_request_id:
        logger.error("DSR erasure task failed closed: tenant_id and privacy_request_id are mandatory.")
        return {'status': 'FAILED', 'error': 'tenant_id and privacy_request_id are required'}

    if kwargs.get('db_alias') == 'default':
        logger.error("DSR erasure task rejected: Tenant operations must never execute against Master DB ('default').")
        return {'status': 'FAILED', 'error': "Tenant tasks cannot execute on 'default' database."}

    from .context import tenant_database_context
    from config.routers import TenantRoutingError

    try:
        with tenant_database_context(tenant_id) as db_alias:
            logger.info("Starting DSR erasure task for tenant '%s', request '%s' on alias '%s'", tenant_id, privacy_request_id, db_alias)

            try:
                req = PrivacyRequest.objects.using(db_alias).select_related('user').get(id=privacy_request_id)
            except PrivacyRequest.DoesNotExist:
                logger.error("PrivacyRequest '%s' not found on DB alias '%s' for tenant '%s'", privacy_request_id, db_alias, tenant_id)
                return {'status': 'FAILED', 'error': 'PrivacyRequest not found'}

            req.status = 'IN_PROGRESS'
            req.save(using=db_alias, update_fields=['status', 'updated_at'])

            user = req.user
            original_email = user.email

            # 1. Pseudonymize / Anonymize user PII
            pseudonym = uuid.uuid4().hex[:8]
            user.email = f"anonymized_{pseudonym}@privacy.deleted"
            user.first_name = "Anonymized"
            user.last_name = "User"
            user.phone = ""
            user.avatar_url = ""
            user.is_login_allowed = False
            user.status = 'INACTIVE'
            user.save(using=db_alias, update_fields=[
                'email', 'first_name', 'last_name', 'phone', 'avatar_url',
                'is_login_allowed', 'status', 'updated_at'
            ])

            # 2. Update ConsentRecords for this user
            ConsentRecord.objects.using(db_alias).filter(user=user, status='GRANTED').update(
                status='WITHDRAWN',
                withdrawn_at=timezone.now(),
                notes='Automatically withdrawn upon DSR erasure request execution.'
            )

            # 3. Complete PrivacyRequest
            now = timezone.now()
            req.status = 'COMPLETED'
            req.completed_at = now
            req.resolution = 'Personal data irreversibly anonymized, consents revoked, and user deactivated in compliance with erasure request.'
            req.evidence = {
                'original_email_redacted': f"{original_email[:2]}***@{original_email.split('@')[-1]}" if '@' in original_email else '***',
                'anonymized_pseudonym': pseudonym,
                'completed_at': now.isoformat(),
            }
            req.save(using=db_alias, update_fields=['status', 'completed_at', 'resolution', 'evidence', 'updated_at'])

            # 4. Emit audit event
            try:
                emit_audit_event(
                    action='DSR_ERASURE_COMPLETED',
                    resource_type='PrivacyRequest',
                    resource_id=req.id,
                    actor_type='SYSTEM_JOB',
                    after_state={'status': 'COMPLETED', 'user_id': str(user.id)},
                    description=f"DSR Erasure anonymized user {pseudonym}.",
                    db_alias=db_alias,
                )
            except Exception as e:
                logger.warning("Failed to emit audit event for DSR erasure: %s", e)

            logger.info("Successfully completed DSR erasure for request '%s'", req.id)
            return {
                'status': 'COMPLETED',
                'privacy_request_id': str(req.id),
                'user_id': str(user.id),
            }
    except TenantRoutingError as tre:
        logger.error("DSR erasure task failed to resolve tenant database context for tenant '%s': %s", tenant_id, tre)
        return {'status': 'FAILED', 'error': str(tre)}


@shared_task(bind=True, acks_late=True, max_retries=3)
def process_domain_outbox_events_task(self, tenant_id: str, limit: int = 50, **kwargs):
    """
    Scans and dispatches pending DomainOutboxEvents within the specified tenant DB.
    Guarantees retry-safe, decoupled asynchronous event delivery.
    """
    if not tenant_id:
        return {'status': 'FAILED', 'error': 'tenant_id is required'}

    try:
        with tenant_database_context(tenant_id) as db_alias:
            from .models_audit_outbox import DomainOutboxEvent

            now = timezone.now()
            events = DomainOutboxEvent.objects.using(db_alias).filter(
                Q(status='PENDING') |
                Q(status='FAILED', next_attempt_at__lte=now)
            ).order_by('created_at')[:limit]

            published = 0
            failed = 0

            for event in events:
                event.status = 'PROCESSING'
                event.save(using=db_alias, update_fields=['status', 'updated_at'])

                try:
                    if event.event_type == 'CRM_TRIAL_BOOKED':
                        from apps.tenant_core.communication.service import CommunicationService
                        from apps.tenant_core.models_crm import TrialBooking
                        trial_id = event.aggregate_id or event.payload.get('trial_id')
                        if trial_id:
                            tb = TrialBooking.objects.using(db_alias).filter(id=trial_id).first()
                            if tb:
                                CommunicationService.send_trial_confirmation(tb)

                    # 2. Generic Automation Engine consumption
                    try:
                        from apps.tenant_core.automation.engine import AutomationEngine
                        AutomationEngine.handle_domain_event(event, db_alias=db_alias)
                    except Exception as auto_err:
                        logger.error("AutomationEngine error processing event %s: %s", event.id, auto_err, exc_info=True)

                    logger.info(
                        "Dispatched domain outbox event %s (%s) for aggregate %s:%s",
                        event.id, event.event_type, event.aggregate_type, event.aggregate_id
                    )
                    event.status = 'PUBLISHED'
                    event.published_at = timezone.now()
                    event.save(using=db_alias, update_fields=['status', 'published_at', 'updated_at'])
                    published += 1
                except Exception as exc:
                    logger.error("Failed to dispatch outbox event %s: %s", event.id, exc)
                    event.attempt_count += 1
                    event.last_error = str(exc)
                    if event.attempt_count >= 5:
                        event.status = 'FAILED'
                    else:
                        backoff = 2 ** event.attempt_count * 10
                        event.next_attempt_at = timezone.now() + timedelta(seconds=backoff)
                        event.status = 'FAILED'
                    event.save(using=db_alias, update_fields=['attempt_count', 'last_error', 'next_attempt_at', 'status', 'updated_at'])
                    failed += 1

            return {
                'status': 'COMPLETED',
                'tenant_id': str(tenant_id),
                'published': published,
                'failed': failed,
            }
    except TenantRoutingError as tre:
        logger.error("Outbox task failed to resolve tenant context for tenant '%s': %s", tenant_id, tre)
        return {'status': 'FAILED', 'error': str(tre)}


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_trial_reminders_periodic_task',
    max_retries=1,
    acks_late=True,
)
def process_trial_reminders_periodic_task(self, tenant_id: str = None, **kwargs) -> dict:
    """
    Periodic task to scan and execute due trial reminders for a tenant.
    Dispatches notifications based on active CRMTrialReminderPolicy and TrialBooking.
    """
    if not tenant_id:
        return {'status': 'FAILED', 'error': 'tenant_id is mandatory'}

    from .context import tenant_database_context
    from .communication.service import CommunicationService

    with tenant_database_context(tenant_id) as db_alias:
        dispatched = CommunicationService.process_due_trial_reminders()
        return {
            'status': 'COMPLETED',
            'tenant_id': str(tenant_id),
            'dispatched_count': len(dispatched),
        }


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_due_waiting_automations_periodic_task',
    max_retries=1,
    acks_late=True,
)
def process_due_waiting_automations_periodic_task(self, tenant_id: str = None, **kwargs) -> dict:
    """
    Periodic task to scan and resume due WAITING automation step executions.
    Safe across worker crashes and multiple workers using SELECT FOR UPDATE SKIP LOCKED.
    """
    if not tenant_id:
        return {'status': 'FAILED', 'error': 'tenant_id is mandatory'}

    from .context import tenant_database_context
    from apps.tenant_core.automation.engine import AutomationEngine

    with tenant_database_context(tenant_id) as db_alias:
        resumed = AutomationEngine.resume_due_waiting_executions(db_alias=db_alias)
        return {
            'status': 'COMPLETED',
            'tenant_id': str(tenant_id),
            'resumed_count': resumed,
        }


