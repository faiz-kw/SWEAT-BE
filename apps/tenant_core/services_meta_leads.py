"""Tenant-owned Meta lead services: Simulator & Production Live Adapter.

Supports:
- Safe, isolated simulator execution for local development & testing.
- Production Graph API v21.0 lead retrieval, campaign attribution, and CRM ingestion.
- Durable idempotency and background retry protection.
"""
import logging
from typing import Tuple
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from config.routers import get_tenant_db_alias
from .meta_lead_rules import map_answers, payload_digest
from .models_meta_leads import MetaLeadMapping, MetaLeadImport, MetaConnection, MetaPageConnection
from .models_org import Branch, Organization
from .models_crm import Lead
from .services_crm import CRMLeadService, validate_lead_email, validate_lead_phone
from .services_reliability import record_business_audit
from .meta_crypto import decrypt_token
from .services_meta_graph import (
    MetaGraphClient,
    MetaRateLimitError,
    MetaTokenExpiredError,
    MetaPermissionError,
    MetaLeadNotFoundError,
    MetaGraphAPIError,
)

from .meta_logging import (
    log_meta_event,
    EVT_GRAPH_FETCH_SUCCESS,
    EVT_GRAPH_FETCH_FAILED,
    EVT_MAPPING_FOUND,
    EVT_MAPPING_MISSING,
    EVT_LEAD_CREATED,
    EVT_DUPLICATE_IGNORED,
    EVT_IMPORT_QUEUED,
)

logger = logging.getLogger(__name__)


def require_tenant_alias():
    alias = get_tenant_db_alias()
    if not alias or alias == 'default':
        raise PermissionDenied('A verified tenant database context is required.')
    return alias


def simulator_enabled():
    return bool(
        getattr(settings, 'META_LEAD_SIMULATOR_ENABLED', False)
        and settings.DEBUG
        and getattr(settings, 'DEPLOYMENT_ENVIRONMENT', '') in ('development', 'test')
        and not getattr(settings, 'COMMUNICATIONS_OUTBOUND_ENABLED', True)
    )


def require_simulator():
    if not simulator_enabled():
        raise PermissionDenied('Simulation is available only in an explicitly enabled development/test deployment with outbound communications disabled.')


# ---------------------------------------------------------------------------
# Simulator Workflows
# ---------------------------------------------------------------------------

def receive_simulation(organization, payload, actor_user=None):
    require_simulator()
    alias = require_tenant_alias()
    digest = payload_digest(payload)
    # Receipt commits before processing. Unique constraint arbitrates duplicate requests.
    with transaction.atomic(using=alias):
        event, created = MetaLeadImport.objects.using(alias).get_or_create(
            organization=organization, mode='SIMULATOR', external_lead_id=payload['external_lead_id'],
            defaults={'page_id': payload['page_id'], 'form_id': payload['form_id'],
                      'field_data': payload['field_data'], 'payload_hash': digest},
        )
        if event.payload_hash != digest:
            raise ValidationError({'external_lead_id': 'This submission ID already exists with different answers or routing identifiers. Use a new ID for a new enquiry.'})
        if created:
            record_business_audit(organization=organization, module='crm', action_code='META_SIMULATION_RECEIVED',
                                  entity_type='MetaLeadImport', entity_id=event.id, actor_user=actor_user,
                                  metadata={'mode': 'SIMULATOR'}, db_alias=alias)
    # Duplicate delivery is a no-op; retry is a separate deliberate action.
    if created:
        event = process_simulation(organization, event.id, actor_user)
    return event, created


def process_simulation(organization, event_id, actor_user=None):
    require_simulator()
    alias = require_tenant_alias()
    with transaction.atomic(using=alias):
        event = MetaLeadImport.objects.using(alias).select_for_update().get(id=event_id, organization=organization, mode='SIMULATOR')
        if event.status == 'IMPORTED':
            return event
        event.attempt_count += 1
        event.error_code = event.error_message = ''
        event.processed_at = None
        mapping = MetaLeadMapping.objects.using(alias).filter(
            organization=organization, page_id=event.page_id, form_id=event.form_id, is_active=True,
        ).select_related('lead_source', 'branch', 'fallback_branch', 'assigned_sales_user').first()
        if not mapping:
            event.status = 'NEEDS_MAPPING'
            event.error_code = 'MAPPING_MISSING'
            event.error_message = 'No active mapping matches this Page and Form. Configure and activate the mapping, then retry.'
        else:
            event.mapping_version = mapping.version
            event.mapping_snapshot = {key: getattr(mapping, key) for key in (
                'field_mappings', 'field_defaults', 'branch_mode', 'branch_field', 'branch_answers',
                'unmatched_branch_policy', 'repeat_policy', 'initial_stage', 'assignment_mode',
                'create_followup_task', 'followup_task_type', 'followup_due_hours')}
            event.mapping_snapshot.update(
                branch_id=str(mapping.branch_id) if mapping.branch_id else None,
                fallback_branch_id=str(mapping.fallback_branch_id) if getattr(mapping, 'fallback_branch_id', None) else None,
                assigned_sales_user_id=str(mapping.assigned_sales_user_id) if getattr(mapping, 'assigned_sales_user_id', None) else None,
                lead_source_id=str(mapping.lead_source_id))
            try:
                # Nested savepoint rolls back every CRM write on failure while retaining the receipt.
                with transaction.atomic(using=alias):
                    _apply_mapping(event, mapping, alias, actor_user, is_live=False)
            except (ValueError, ValidationError, DjangoValidationError) as exc:
                event.status, event.error_code = 'FAILED', 'INVALID_LEAD_DATA'
                event.error_message = f'Mapped lead data was rejected: {exc}. Check name, contact format, field lengths and assignment policy.'
            except Exception as exc:
                # Do not expose provider credentials, payloads or exception text in logs/UI.
                logger.error('Meta simulation processing failed; import_id=%s error_type=%s', event.id, type(exc).__name__)
                event.status, event.error_code, event.error_message = 'FAILED', 'PROCESSING_ERROR', 'Import failed. Review server diagnostics and retry; no partial CRM changes were committed.'
        event.save(using=alias)
        record_business_audit(organization=organization, module='crm', action_code='META_SIMULATION_PROCESSED',
                              entity_type='MetaLeadImport', entity_id=event.id, actor_user=actor_user,
                              metadata={'status': event.status, 'attempt': event.attempt_count, 'mapping_version': event.mapping_version}, db_alias=alias)
        return event


# ---------------------------------------------------------------------------
# Production Live Webhook Ingestion & Graph API Processing
# ---------------------------------------------------------------------------

def receive_live_webhook_event(organization, page_id: str, form_id: str, leadgen_id: str, raw_payload: dict, alias: str = None) -> Tuple[MetaLeadImport, bool]:
    """
    Durable live event receipt.
    Persists event as PENDING before background processing begins.
    Database unique constraint on (organization, mode='LIVE', external_lead_id) enforces idempotency.
    """
    if not alias:
        alias = require_tenant_alias()
    digest = payload_digest(raw_payload) if raw_payload else ''

    with transaction.atomic(using=alias):
        event, created = MetaLeadImport.objects.using(alias).get_or_create(
            organization=organization,
            mode='LIVE',
            external_lead_id=str(leadgen_id),
            defaults={
                'page_id': str(page_id),
                'form_id': str(form_id),
                'field_data': [],
                'payload_hash': digest,
                'status': 'PENDING',
            },
        )
        if created:
            record_business_audit(
                organization=organization, module='crm', action_code='META_WEBHOOK_RECEIVED',
                entity_type='MetaLeadImport', entity_id=event.id, actor_user=None,
                metadata={'mode': 'LIVE', 'page_id': page_id, 'form_id': form_id, 'leadgen_id': leadgen_id},
                db_alias=alias,
            )
        else:
            logger.info("Duplicate live leadgen event ignored: page=%s leadgen_id=%s", page_id, leadgen_id)

    return event, created


def process_live_import(organization, event_id, actor_user=None, graph_client=None, alias: str = None) -> MetaLeadImport:
    """
    Fetches lead details from Meta Graph API using the decrypted Page Access Token,
    parses field answers and campaign attribution, and commits CRM records atomically.
    """
    if not alias:
        alias = require_tenant_alias()
    if not graph_client:
        graph_client = MetaGraphClient()

    with transaction.atomic(using=alias):
        event = MetaLeadImport.objects.using(alias).select_for_update().get(
            id=event_id, organization=organization, mode='LIVE'
        )
        if event.status == 'IMPORTED':
            return event

        event.attempt_count += 1
        event.error_code = event.error_message = ''
        event.processed_at = None

        # 1. Resolve Page Connection and Decrypt Token
        page_conn = MetaPageConnection.objects.using(alias).filter(
            organization=organization, page_id=event.page_id, is_active=True
        ).first()

        if not page_conn or not page_conn.encrypted_page_access_token:
            event.status = 'FAILED'
            event.error_code = 'PAGE_NOT_CONNECTED'
            event.error_message = 'Page access token is missing or page connection is inactive. Reconnect the Meta account.'
            event.save(using=alias)
            return event

        page_token = decrypt_token(page_conn.encrypted_page_access_token)
        if not page_token:
            event.status = 'FAILED'
            event.error_code = 'TOKEN_DECRYPTION_FAILED'
            event.error_message = 'Failed to decrypt Page access token. Re-authorize Meta connection.'
            event.save(using=alias)
            return event

        # 2. Fetch full lead details from Meta Graph API if field_data is empty
        if not event.field_data:
            try:
                lead_data = graph_client.fetch_leadgen_details(event.external_lead_id, page_token)
                event.field_data = lead_data.get('field_data', [])
                if not event.form_id and lead_data.get('form_id'):
                    event.form_id = lead_data['form_id']
                event.campaign_id = str(lead_data.get('campaign_id') or '')
                event.campaign_name = str(lead_data.get('campaign_name') or '')
                event.adset_id = str(lead_data.get('adset_id') or '')
                event.adset_name = str(lead_data.get('adset_name') or '')
                event.ad_id = str(lead_data.get('ad_id') or '')
                event.ad_name = str(lead_data.get('ad_name') or '')
                event.is_organic = bool(lead_data.get('is_organic', False))
                log_meta_event(
                    EVT_GRAPH_FETCH_SUCCESS,
                    "Retrieved lead details from Meta Graph API",
                    import_id=str(event.id),
                    page_id=event.page_id,
                    form_id=event.form_id,
                    leadgen_id=event.external_lead_id,
                )
            except MetaRateLimitError as exc:
                event.status = 'FAILED'
                event.error_code = 'RATE_LIMITED'
                event.error_message = str(exc)
                event.save(using=alias)
                raise  # Re-raise so Celery retries with exponential backoff
            except MetaTokenExpiredError as exc:
                event.status = 'FAILED'
                event.error_code = 'TOKEN_EXPIRED'
                event.error_message = 'Meta Page access token has expired or was revoked. Reconnect account.'
                # Update connection state
                MetaConnection.objects.using(alias).filter(organization=organization).update(
                    status='TOKEN_EXPIRED', last_error='Access token invalidated by Meta'
                )
                event.save(using=alias)
                return event
            except MetaLeadNotFoundError as exc:
                event.status = 'FAILED'
                event.error_code = 'LEAD_NOT_FOUND'
                event.error_message = 'Lead was deleted or expired on Meta servers.'
                event.save(using=alias)
                return event
            except Exception as exc:
                logger.error("Meta Graph API lead retrieval error for import %s: %s", event.id, exc)
                event.status = 'FAILED'
                event.error_code = 'GRAPH_API_ERROR'
                event.error_message = 'Failed to fetch lead data from Meta Graph API. Review connection.'
                event.save(using=alias)
                raise

        # 3. Match configured form mapping
        mapping = MetaLeadMapping.objects.using(alias).filter(
            organization=organization, page_id=event.page_id, form_id=event.form_id, is_active=True,
        ).select_related('lead_source', 'branch', 'fallback_branch', 'assigned_sales_user').first()

        if not mapping:
            event.status = 'NEEDS_MAPPING'
            event.error_code = 'MAPPING_MISSING'
            event.error_message = f"No active mapping matches Page '{event.page_id}' and Form '{event.form_id}'. Configure and activate mapping to import."
            event.save(using=alias)
            log_meta_event(
                EVT_MAPPING_MISSING,
                f"No active mapping matches Page '{event.page_id}' and Form '{event.form_id}'",
                import_id=str(event.id),
                page_id=event.page_id,
                form_id=event.form_id,
                status='NEEDS_MAPPING',
                level=logging.WARNING,
            )
            return event

        log_meta_event(
            EVT_MAPPING_FOUND,
            f"Matched active form mapping: {mapping.name}",
            import_id=str(event.id),
            page_id=event.page_id,
            form_id=event.form_id,
            extra={'mapping_id': str(mapping.id)},
        )

        event.mapping_version = mapping.version
        event.mapping_snapshot = {key: getattr(mapping, key) for key in (
            'field_mappings', 'field_defaults', 'branch_mode', 'branch_field', 'branch_answers',
            'unmatched_branch_policy', 'repeat_policy', 'initial_stage', 'assignment_mode',
            'create_followup_task', 'followup_task_type', 'followup_due_hours')}
        event.mapping_snapshot.update(
            branch_id=str(mapping.branch_id) if mapping.branch_id else None,
            fallback_branch_id=str(mapping.fallback_branch_id) if getattr(mapping, 'fallback_branch_id', None) else None,
            assigned_sales_user_id=str(mapping.assigned_sales_user_id) if getattr(mapping, 'assigned_sales_user_id', None) else None,
            lead_source_id=str(mapping.lead_source_id))

        # 4. Ingest into CRM within isolated savepoint
        try:
            with transaction.atomic(using=alias):
                _apply_mapping(event, mapping, alias, actor_user, is_live=True)
        except (ValueError, ValidationError, DjangoValidationError) as exc:
            event.status, event.error_code = 'FAILED', 'INVALID_LEAD_DATA'
            event.error_message = f'Mapped lead data was rejected: {exc}.'
        except Exception as exc:
            logger.error('Meta live lead import failed; import_id=%s error_type=%s', event.id, type(exc).__name__)
            event.status, event.error_code, event.error_message = 'FAILED', 'PROCESSING_ERROR', 'Import failed. No partial CRM changes were committed.'

        event.save(using=alias)
        record_business_audit(
            organization=organization, module='crm', action_code='META_IMPORT_PROCESSED',
            entity_type='MetaLeadImport', entity_id=event.id, actor_user=actor_user,
            metadata={'status': event.status, 'attempt': event.attempt_count, 'mapping_version': event.mapping_version, 'mode': 'LIVE'},
            db_alias=alias,
        )
        return event


# ---------------------------------------------------------------------------
# Core Mapping and CRM Ingestion Engine
# ---------------------------------------------------------------------------

def _apply_mapping(event, mapping, alias, actor_user, is_live: bool = False):
    from datetime import timedelta
    from .models_crm import SalesFollowupTask

    # Serialise contact-policy evaluation among imports for this organisation.
    Organization.objects.using(alias).select_for_update().get(id=event.organization_id, status='ACTIVE')
    mapped, answers = map_answers(event.field_data, mapping.field_mappings, getattr(mapping, 'field_defaults', {}))
    if mapping.lead_source.organization_id != event.organization_id or mapping.lead_source.status != 'ACTIVE' or mapping.lead_source.source_type != 'META':
        raise ValueError('An active Meta lead source is required.')

    branch_id = mapping.branch_id
    if mapping.branch_mode == 'ANSWER':
        values = answers.get(mapping.branch_field, [])
        answer = values[0].strip().casefold() if len(values) == 1 else ''
        branch_id = mapping.branch_answers.get(answer)
        if not branch_id and getattr(mapping, 'unmatched_branch_policy', 'HOLD') == 'FALLBACK_BRANCH':
            branch_id = getattr(mapping, 'fallback_branch_id', None)

    branch = Branch.objects.using(alias).filter(id=branch_id, organization_id=event.organization_id, status='ACTIVE').first() if branch_id else None
    if not branch:
        event.status, event.error_code, event.error_message = 'NEEDS_ASSIGNMENT', 'BRANCH_UNRESOLVED', 'No active branch matches the saved mapping. The enquiry is held for review.'
        return

    email = validate_lead_email(mapped.get('email'), required=False)
    phone = validate_lead_phone(mapped.get('phone'), required=False)
    contact = Q()
    if email:
        contact |= Q(email_normalized=email)
    if phone:
        contact |= Q(phone_normalized=phone)
    if mapping.repeat_policy == 'REVIEW' and Lead.objects.using(alias).filter(organization_id=event.organization_id).filter(contact).exists():
        event.status, event.error_code, event.error_message = 'NEEDS_REVIEW', 'EXISTING_CONTACT', 'This contact already has a lead. The new enquiry is retained here. Review it before changing the repeat-enquiry policy and retrying.'
        return

    # Extract supported extra native lead fields
    bool_fields = {'consent_whatsapp', 'consent_email', 'consent_sms'}
    extra = {}
    for k, v in mapped.items():
        if k in ('fitness_goal', 'area', 'country', 'gender', 'occupation', 'company_name', 'date_of_birth') and v:
            extra[k] = v
        elif k in bool_fields and v != '':
            extra[k] = str(v).strip().lower() in ('true', '1', 'yes', 'y')

    # Validate model field lengths before PostgreSQL would reject the insert.
    for key in ('first_name', 'last_name', *(k for k in extra if k not in bool_fields)):
        value = mapped.get(key, '')
        if isinstance(value, str):
            field = Lead._meta.get_field(key)
            maximum = getattr(field, 'max_length', None)
            if maximum and len(value) > maximum:
                raise ValueError(f'Mapped answer for {key} exceeds CRM field length of {maximum}.')

    # Assigned sales user preference
    assigned_sales_user = None
    if getattr(mapping, 'assignment_mode', 'TENANT_POLICY') == 'SPECIFIC_USER' and getattr(mapping, 'assigned_sales_user_id', None):
        assigned_sales_user = mapping.assigned_sales_user

    if is_live:
        attribution_data = {
            'platform': 'META',
            'external_lead_id': event.external_lead_id,
            'form_external_id': event.form_id,
            'campaign_id': event.campaign_id or None,
            'campaign_name': event.campaign_name or None,
            'adset_id': event.adset_id or None,
            'ad_id': event.ad_id or None,
            'raw_metadata': {
                'is_test': False,
                'import_id': str(event.id),
                'ad_name': event.ad_name,
                'adset_name': event.adset_name,
                'page_id': event.page_id,
                'is_organic': event.is_organic,
            },
        }
    else:
        attribution_data = {
            'platform': 'META',
            'external_lead_id': f'simulation:{event.id}',
            'form_external_id': event.form_id,
            'raw_metadata': {'is_test': True, 'import_id': str(event.id)},
        }

    lead = CRMLeadService.create_lead(
        organization=event.organization, first_name=mapped['first_name'], last_name=mapped['last_name'],
        email=email, phone=phone, branch=branch, lead_source=mapping.lead_source,
        assigned_sales_user=assigned_sales_user,
        actor_user=actor_user, extra_fields=extra,
        attribution_data=attribution_data,
        db_alias=alias,
    )

    # Initial pipeline stage transition if configured beyond NEW_LEAD
    initial_stage = getattr(mapping, 'initial_stage', 'NEW_LEAD')
    if initial_stage and initial_stage != 'NEW_LEAD':
        CRMLeadService.transition_lead_status(
            lead=lead,
            new_status=initial_stage,
            reason_code='META_INITIAL_STAGE',
            reason_text=f'Initial stage configured by Meta Lead mapping: {mapping.name}',
            actor_user=actor_user,
            db_alias=alias,
        )

    # Automated follow-up task creation if configured
    if getattr(mapping, 'create_followup_task', False):
        task_agent = lead.assigned_sales_user or actor_user
        if task_agent:
            due_hours = getattr(mapping, 'followup_due_hours', 24) or 24
            due_at = timezone.now() + timedelta(hours=due_hours)
            task_type = getattr(mapping, 'followup_task_type', 'CALL') or 'CALL'
            idempotency_ref = f"META_IMPORT_TASK_{event.id}"
            if not SalesFollowupTask.objects.using(alias).filter(external_reference=idempotency_ref).exists():
                SalesFollowupTask.objects.using(alias).create(
                    lead=lead,
                    task_type=task_type,
                    priority='HIGH',
                    due_at=due_at,
                    status='PENDING',
                    assigned_to_user=task_agent,
                    created_by_user=actor_user or task_agent,
                    external_reference=idempotency_ref,
                    outcome=f"Meta Lead initial outreach for form '{mapping.name}'. Due within {due_hours}h.",
                )

    event.lead = lead
    event.status = 'IMPORTED'
    event.processed_at = timezone.now()
