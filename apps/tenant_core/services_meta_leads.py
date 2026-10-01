"""Durable development imports. No external API calls or cross-database commits."""
import logging
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from config.routers import get_tenant_db_alias
from .meta_lead_rules import map_answers, payload_digest
from .models_meta_leads import MetaLeadMapping, MetaLeadImport
from .models_org import Branch, Organization
from .models_crm import Lead
from .services_crm import CRMLeadService, validate_lead_email, validate_lead_phone
from .services_reliability import record_business_audit

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
        ).select_related('lead_source').first()
        if not mapping:
            event.status, event.error_code, event.error_message = 'NEEDS_MAPPING', 'MAPPING_MISSING', 'Configure and enable this Page/form mapping, then retry.'
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
                    _apply_mapping(event, mapping, alias, actor_user)
            except (ValueError, ValidationError, DjangoValidationError):
                event.status, event.error_code = 'FAILED', 'INVALID_LEAD_DATA'
                event.error_message = 'Mapped lead data was rejected. Check name, contact format, field lengths and assignment policy. Current CRM phone validation supports Indian mobile numbers.'
            except Exception as exc:
                # Do not expose provider credentials, payloads or exception text in logs/UI.
                logger.error('Meta simulation processing failed; import_id=%s error_type=%s', event.id, type(exc).__name__)
                event.status, event.error_code, event.error_message = 'FAILED', 'PROCESSING_ERROR', 'Import failed. Review server diagnostics and retry; no partial CRM changes were committed.'
        event.save(using=alias)
        record_business_audit(organization=organization, module='crm', action_code='META_SIMULATION_PROCESSED',
                              entity_type='MetaLeadImport', entity_id=event.id, actor_user=actor_user,
                              metadata={'status': event.status, 'attempt': event.attempt_count, 'mapping_version': event.mapping_version}, db_alias=alias)
        return event


def _apply_mapping(event, mapping, alias, actor_user):
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

    lead = CRMLeadService.create_lead(
        organization=event.organization, first_name=mapped['first_name'], last_name=mapped['last_name'],
        email=email, phone=phone, branch=branch, lead_source=mapping.lead_source,
        assigned_sales_user=assigned_sales_user,
        actor_user=actor_user, extra_fields=extra,
        attribution_data={'platform': 'META', 'external_lead_id': f'simulation:{event.id}',
                          'form_external_id': event.form_id, 'raw_metadata': {'is_test': True, 'import_id': str(event.id)}},
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
