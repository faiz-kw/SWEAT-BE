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
from django.core.serializers.json import DjangoJSONEncoder

from .models_privacy import PrivacyRequest, ConsentRecord, TenantAuditEvent
from .models_users import TenantUser
from .models_infra import File
from .audit import emit_audit_event

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_dsr_export_task',
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,
)
def process_dsr_export_task(self, privacy_request_id: str, db_alias: str = 'default') -> dict:
    """
    Executes DSR Right of Access / Data Portability export workflow asynchronously.
    """
    logger.info("Starting DSR export task for request '%s' on alias '%s'", privacy_request_id, db_alias)

    try:
        req = PrivacyRequest.objects.using(db_alias).select_related('user').get(id=privacy_request_id)
    except PrivacyRequest.DoesNotExist:
        logger.error("PrivacyRequest '%s' not found on DB alias '%s'", privacy_request_id, db_alias)
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


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks.process_dsr_erasure_task',
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,
)
def process_dsr_erasure_task(self, privacy_request_id: str, db_alias: str = 'default') -> dict:
    """
    Executes DSR Right to Erasure / Deletion workflow asynchronously.
    Irreversibly anonymizes user PII while preserving required immutable legal audit history.
    """
    logger.info("Starting DSR erasure task for request '%s' on alias '%s'", privacy_request_id, db_alias)

    try:
        req = PrivacyRequest.objects.using(db_alias).select_related('user').get(id=privacy_request_id)
    except PrivacyRequest.DoesNotExist:
        logger.error("PrivacyRequest '%s' not found on DB alias '%s'", privacy_request_id, db_alias)
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
