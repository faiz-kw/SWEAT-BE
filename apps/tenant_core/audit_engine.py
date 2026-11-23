"""
apps/tenant_core/audit_engine.py — Centralized Unified Activity & Audit Logging Engine.

Core Responsibilities:
1. Unified API: `UnifiedAuditEngine.record_activity(...)`
2. State Diff Engine: Granular field-by-field model comparison producing structured `field_changes`.
3. Sensitive Data Sanitizer: Recursive key pattern masking (passwords, tokens, credentials, CVV, OTP).
4. Narrative Synthesizer: 9-point contract narrative generator answering:
   - Who performed the activity?
   - What exactly did they do?
   - Which CRM module/section was involved?
   - Which customer/record was affected?
   - What was the previous value/status?
   - What is the new value/status?
   - When did it happen?
   - Was it successful or failed?
   - Additional details, reasons, remarks?
5. Backwards Compatibility: Native adapters for legacy `emit_audit_event` and `record_business_audit`.
"""

import uuid
import logging
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional, Dict, List, Tuple

from django.db import models
from django.utils import timezone
from django.core.exceptions import PermissionDenied

from config.routers import get_tenant_db_alias
from .models_activity_audit import ActivityAuditLog
from .models_org import Organization, Branch
from .models_users import TenantUser

logger = logging.getLogger(__name__)

# Thread-local flag tracking whether an explicit domain audit log was recorded during the active HTTP request
_request_audit_state = threading.local()


def reset_request_audit_flag() -> None:
    _request_audit_state.recorded = False


def mark_request_audited() -> None:
    _request_audit_state.recorded = True


def was_request_audited() -> bool:
    return bool(getattr(_request_audit_state, 'recorded', False))

# Sensitive key patterns that must be redacted recursively
SENSITIVE_KEY_PATTERNS = {
    'password', 'password_hash', 'token', 'access_token', 'refresh_token',
    'secret', 'secret_key', 'api_key', 'authorization', 'cookie', 'cvv',
    'credit_card', 'card_number', 'pan', 'mfa', 'otp', 'private_key',
    'pass_hash', 'auth_token', 'session_key'
}

# Fields to ignore when computing model diffs (internal metadata/timestamps)
IGNORED_DIFF_FIELDS = {
    'created_at', 'updated_at', 'modified_at', 'last_modified',
    'password', 'password_hash', 'sync_hash'
}

# Friendly field label mappings for simple English presentation
FRIENDLY_FIELD_LABELS = {
    'first_name': 'First Name',
    'last_name': 'Last Name',
    'email': 'Email Address',
    'phone': 'Phone Number',
    'phone_number': 'Phone Number',
    'status': 'Status',
    'is_active': 'Active Status',
    'role': 'Role',
    'branch': 'Branch',
    'branch_id': 'Branch',
    'home_branch': 'Home Branch',
    'home_branch_id': 'Home Branch',
    'department': 'Department',
    'title': 'Title',
    'name': 'Name',
    'price': 'Price',
    'amount': 'Amount',
    'start_date': 'Start Date',
    'end_date': 'End Date',
    'freeze_start_date': 'Freeze Start Date',
    'freeze_end_date': 'Freeze End Date',
    'cancellation_reason': 'Cancellation Reason',
    'capacity': 'Capacity',
    'trainer': 'Assigned Trainer',
    'notes': 'Notes',
    'payment_status': 'Payment Status',
    'emergency_contact_phone': 'Emergency Contact Phone',
    'gender': 'Gender',
    'date_of_birth': 'Date of Birth',
}


def sanitize_state(data: Any) -> Any:
    """
    Recursively sanitize any python structure (dict, list, primitive, model).
    Replaces values of sensitive keys with '[REDACTED]'.
    Serializes datetimes, dates, UUIDs, and Decimals to JSON-safe primitives.
    """
    if data is None:
        return None

    if isinstance(data, dict):
        sanitized = {}
        for key, val in data.items():
            key_str = str(key).lower()
            if any(p in key_str for p in SENSITIVE_KEY_PATTERNS):
                sanitized[key] = '[REDACTED]'
            else:
                sanitized[key] = sanitize_state(val)
        return sanitized

    if isinstance(data, (list, tuple, set)):
        return [sanitize_state(item) for item in data]

    if isinstance(data, (uuid.UUID, date, datetime)):
        return data.isoformat() if hasattr(data, 'isoformat') else str(data)

    if isinstance(data, Decimal):
        return float(data)

    if isinstance(data, (int, float, bool, str)):
        return data

    if hasattr(data, 'pk'):
        return str(data.pk)

    return str(data)


def snapshot_instance(instance: Optional[models.Model]) -> Optional[Dict[str, Any]]:
    """
    Takes a clean dictionary snapshot of a model instance's concrete fields.
    """
    if instance is None or not isinstance(instance, models.Model):
        return None

    state = {}
    for field in instance._meta.concrete_fields:
        field_name = field.name
        val = getattr(instance, field.attname, None)
        if isinstance(val, (uuid.UUID, date, datetime)):
            val = val.isoformat() if hasattr(val, 'isoformat') else str(val)
        elif isinstance(val, Decimal):
            val = float(val)
        state[field_name] = val

    return sanitize_state(state)


def compute_field_changes(
    before_state: Optional[Dict[str, Any]],
    after_state: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Calculates granular field-by-field differences between before_state and after_state.
    Returns:
        [
            {
                "field_name": "phone_number",
                "field_label": "Phone Number",
                "old_value": "+91-9876543210",
                "new_value": "+91-9876543211"
            },
            ...
        ]
    """
    if not before_state and not after_state:
        return []

    before = before_state or {}
    after = after_state or {}
    all_keys = set(before.keys()) | set(after.keys())
    changes = []

    for key in sorted(all_keys):
        key_lower = str(key).lower()
        if key in IGNORED_DIFF_FIELDS or any(pat in key_lower for pat in SENSITIVE_KEY_PATTERNS):
            continue

        old_val = before.get(key)
        new_val = after.get(key)

        # Skip if identical
        if old_val == new_val:
            continue

        # Generate human friendly label
        field_label = FRIENDLY_FIELD_LABELS.get(
            key,
            key.replace('_', ' ').strip().title()
        )

        changes.append({
            'field_name': key,
            'field_label': field_label,
            'old_value': sanitize_state(old_val),
            'new_value': sanitize_state(new_val),
        })

    return changes


def synthesize_narrative(
    actor_name: str,
    actor_role: str,
    action_verb: str,
    module: str,
    entity_type: str,
    entity_name: str,
    field_changes: Optional[List[Dict[str, Any]]] = None,
    result: str = 'SUCCESS',
    reason: str = '',
    remarks: str = '',
    error_message: str = '',
) -> str:
    """
    Synthesizes a clean, simple, human-readable English narrative answering the 9 questions.
    """
    actor_desc = f"{actor_role} {actor_name}".strip() if actor_role and actor_name else (actor_name or actor_role or "System")
    verb_display = (action_verb or "modified").strip().lower().replace('_', ' ')
    entity_display = f"{entity_type} '{entity_name}'" if entity_name else entity_type

    if result == 'FAILURE':
        # Avoid awkward phrasing like "attempted to failed to log in" or "attempted to recorded..."
        already_failure_prefixes = ('failed', 'denied', 'rejected', 'blocked', 'recorded', 'could not')
        if verb_display.startswith(already_failure_prefixes):
            narrative = f"{actor_desc} {verb_display} for {entity_display} in {module}."
        else:
            infinitive_map = {
                'created': 'create', 'updated': 'update', 'deleted': 'delete',
                'modified': 'modify', 'froze': 'freeze', 'unfroze': 'unfreeze',
                'cancelled': 'cancel', 'extended': 'extend', 'transferred': 'transfer',
                'booked': 'book', 'logged in': 'log in', 'logged out': 'log out',
                'revoked': 'revoke', 'assigned': 'assign', 'removed': 'remove',
            }
            infinitive = infinitive_map.get(verb_display, verb_display)
            narrative = f"{actor_desc} attempted to {infinitive} {entity_display} in {module}, but the operation failed."
        if error_message:
            clean_err = str(error_message).strip().rstrip('.')
            narrative += f" Error: {clean_err}."
        if reason:
            clean_reason = str(reason).strip().rstrip('.')
            narrative += f" Reason: {clean_reason}."
        return narrative.strip()

    narrative = f"{actor_desc} {verb_display} {entity_display} in {module}."

    if field_changes and len(field_changes) > 0:
        if len(field_changes) <= 3:
            diff_strs = [
                f"{c['field_label']} from '{c['old_value']}' to '{c['new_value']}'"
                for c in field_changes
            ]
            narrative += f" Changed: {', '.join(diff_strs)}."
        else:
            first_two = [
                f"{c['field_label']} from '{c['old_value']}' to '{c['new_value']}'"
                for c in field_changes[:2]
            ]
            remaining_count = len(field_changes) - 2
            narrative += f" Changed: {', '.join(first_two)}, and {remaining_count} other fields."

    if reason:
        clean_reason = str(reason).strip().rstrip('.')
        narrative += f" Reason: {clean_reason}."
    if remarks:
        clean_remarks = str(remarks).strip().rstrip('.')
        narrative += f" Remarks: {clean_remarks}."

    return narrative.strip()


class UnifiedAuditEngine:
    """
    Unified Activity & Audit Engine for the entire CRM.
    """

    @classmethod
    def record_activity(
        cls,
        action_code: str,
        module: str,
        entity_type: str,
        entity_id: Optional[Any] = None,
        entity_name: str = '',
        request=None,
        actor_user: Optional[TenantUser] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        actor_email: str = '',
        actor_name: str = '',
        actor_role: str = '',
        action_category: Optional[str] = None,
        action_verb: Optional[str] = None,
        submodule: str = '',
        instance: Optional[models.Model] = None,
        before_state: Optional[Any] = None,
        after_state: Optional[Any] = None,
        field_changes: Optional[List[Dict[str, Any]]] = None,
        event_description: str = '',
        reason: str = '',
        remarks: str = '',
        result: str = 'SUCCESS',
        error_code: str = '',
        error_message: str = '',
        metadata: Optional[Dict[str, Any]] = None,
        organization: Optional[Organization] = None,
        organization_id: Optional[uuid.UUID] = None,
        branch: Optional[Branch] = None,
        branch_id: Optional[uuid.UUID] = None,
        source_channel: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: str = '',
        request_id: str = '',
        correlation_id: str = '',
        db_alias: Optional[str] = None,
        *args,
        **kwargs,
    ) -> Optional[ActivityAuditLog]:
        """
        Record a comprehensive, immutable activity audit log entry across any CRM module.
        """
        target_db = db_alias or get_tenant_db_alias()
        if not target_db or target_db == 'default':
            logger.warning("Audit record_activity called without active tenant DB. Writing to default blocked.")
            return None

        from config.routers import set_tenant_db_alias
        prev_thread_alias = get_tenant_db_alias()
        if not prev_thread_alias:
            set_tenant_db_alias(target_db)

        try:
            return cls._execute_record_activity(
                action_code=action_code,
                module=module,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                request=request,
                actor_user=actor_user,
                actor_type=actor_type,
                actor_id=actor_id,
                actor_email=actor_email,
                actor_name=actor_name,
                actor_role=actor_role,
                action_category=action_category,
                action_verb=action_verb,
                submodule=submodule,
                instance=instance,
                before_state=before_state,
                after_state=after_state,
                field_changes=field_changes,
                event_description=event_description,
                reason=reason,
                remarks=remarks,
                result=result,
                error_code=error_code,
                error_message=error_message,
                metadata=metadata,
                organization=organization,
                organization_id=organization_id,
                branch=branch,
                branch_id=branch_id,
                source_channel=source_channel,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                correlation_id=correlation_id,
                target_db=target_db,
                *args,
                **kwargs,
            )
        finally:
            if not prev_thread_alias:
                set_tenant_db_alias(prev_thread_alias)

    @classmethod
    def _execute_record_activity(
        cls,
        action_code: str,
        module: str,
        entity_type: str,
        entity_id: Optional[Any] = None,
        entity_name: str = '',
        request=None,
        actor_user: Optional[TenantUser] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        actor_email: str = '',
        actor_name: str = '',
        actor_role: str = '',
        action_category: Optional[str] = None,
        action_verb: Optional[str] = None,
        submodule: str = '',
        instance: Optional[models.Model] = None,
        before_state: Optional[Any] = None,
        after_state: Optional[Any] = None,
        field_changes: Optional[List[Dict[str, Any]]] = None,
        event_description: str = '',
        reason: str = '',
        remarks: str = '',
        result: str = 'SUCCESS',
        error_code: str = '',
        error_message: str = '',
        metadata: Optional[Dict[str, Any]] = None,
        organization: Optional[Organization] = None,
        organization_id: Optional[uuid.UUID] = None,
        branch: Optional[Branch] = None,
        branch_id: Optional[uuid.UUID] = None,
        source_channel: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: str = '',
        request_id: str = '',
        correlation_id: str = '',
        target_db: str = '',
        *args,
        **kwargs,
    ) -> Optional[ActivityAuditLog]:

        # 1. Resolve Entity Details from Instance if passed
        resolved_entity_id = entity_id
        resolved_entity_name = entity_name
        if instance is not None and isinstance(instance, models.Model):
            if not resolved_entity_id:
                resolved_entity_id = getattr(instance, 'pk', None)
            if not resolved_entity_name:
                resolved_entity_name = str(
                    getattr(instance, 'name', None) or
                    getattr(instance, 'title', None) or
                    getattr(instance, 'email', None) or
                    f"{getattr(instance, 'first_name', '')} {getattr(instance, 'last_name', '')}".strip() or
                    getattr(instance, 'pk', '')
                )

        # 2. Resolve Actor Context
        res_actor_user = actor_user
        res_actor_type = actor_type or ('STAFF' if res_actor_user else 'SYSTEM')
        res_actor_id = actor_id or (str(res_actor_user.id) if res_actor_user else None)
        res_actor_email = actor_email or (getattr(res_actor_user, 'email', '') if res_actor_user else '')
        res_actor_name = actor_name or ''
        res_actor_role = actor_role or ''

        # If request is provided and actor is not fully populated, derive from request
        if request is not None:
            req_user = getattr(request, 'user', None)
            if req_user and getattr(req_user, 'is_authenticated', False):
                if isinstance(req_user, TenantUser):
                    res_actor_user = req_user
                    res_actor_id = str(req_user.id)
                    res_actor_email = req_user.email or ''
                    first = getattr(req_user, 'first_name', '') or ''
                    last = getattr(req_user, 'last_name', '') or ''
                    if not res_actor_name:
                        res_actor_name = f"{first} {last}".strip() or res_actor_email
                    if not res_actor_role:
                        res_actor_role = getattr(req_user, 'role', '') or getattr(req_user, 'user_type', '') or ''
                        if not res_actor_role or str(res_actor_role).upper() == 'STAFF':
                            try:
                                from .models_rbac import RoleAssignment
                                ra = RoleAssignment.objects.using(target_db).filter(
                                    user_id=req_user.id, is_active=True
                                ).select_related('role').first()
                                if ra and ra.role:
                                    res_actor_role = ra.role.name or ra.role.code
                            except Exception:
                                pass
                        if not res_actor_role:
                            res_actor_role = 'Staff'
                    if not actor_type:
                        u_type = str(getattr(req_user, 'user_type', '') or '').upper()
                        role_upper = str(res_actor_role or '').upper()
                        if getattr(req_user, 'is_superuser', False) or 'ADMIN' in role_upper or 'OWNER' in role_upper:
                            res_actor_type = 'ADMIN'
                        elif u_type == 'MEMBER' or role_upper == 'MEMBER':
                            res_actor_type = 'MEMBER'
                        else:
                            res_actor_type = 'STAFF'
                elif getattr(req_user, '_auth_type', None) == 'platform':
                    res_actor_type = 'ADMIN'
                    res_actor_email = getattr(req_user, 'email', '') or 'platform_admin'
                    res_actor_name = 'Platform Super Admin'
                    res_actor_role = 'Super Admin'

        if not res_actor_name and res_actor_email:
            res_actor_name = res_actor_email.split('@')[0].replace('.', ' ').title()

        # 3. Resolve Organization and Branch Scope
        res_org = organization
        res_branch = branch
        if not res_branch and branch_id:
            try:
                res_branch = Branch.objects.using(target_db).filter(id=branch_id).first()
            except Exception:
                pass

        if not res_branch and instance is not None:
            if isinstance(instance, Branch):
                res_branch = instance
            else:
                br_ref = getattr(instance, 'branch', None) or getattr(instance, 'home_branch', None)
                if isinstance(br_ref, Branch):
                    res_branch = br_ref

        if not res_org:
            if organization_id:
                try:
                    res_org = Organization.objects.using(target_db).filter(id=organization_id).first()
                except Exception:
                    pass
            elif res_branch and getattr(res_branch, 'organization', None):
                res_org = res_branch.organization
            elif instance is not None and getattr(instance, 'organization', None):
                res_org = getattr(instance, 'organization')
            else:
                try:
                    res_org = Organization.objects.using(target_db).first()
                except Exception:
                    pass

        # 4. Infer Action Category and Verb if missing
        res_category = action_category or cls._infer_category(action_code, module)
        res_verb = action_verb or cls._infer_verb(action_code)

        # 5. Snapshot States & Diff Engine
        raw_before = snapshot_instance(before_state) if isinstance(before_state, models.Model) else before_state
        raw_after = snapshot_instance(after_state) if isinstance(after_state, models.Model) else after_state

        sanitized_before = sanitize_state(raw_before)
        sanitized_after = sanitize_state(raw_after)

        res_field_changes = field_changes
        if res_field_changes is None and (sanitized_before or sanitized_after):
            res_field_changes = compute_field_changes(sanitized_before, sanitized_after)

        # 6. Generate Simple English Narrative if not explicitly provided
        final_description = event_description
        if not final_description:
            final_description = synthesize_narrative(
                actor_name=res_actor_name,
                actor_role=res_actor_role,
                action_verb=res_verb,
                module=module,
                entity_type=entity_type,
                entity_name=resolved_entity_name,
                field_changes=res_field_changes,
                result=result,
                reason=reason,
                remarks=remarks,
                error_message=error_message,
            )

        # 7. Request Tracing & Telemetry
        final_request_id = request_id or kwargs.get('request_id', '')
        final_correlation_id = correlation_id or kwargs.get('correlation_id', '')
        final_ip_address = ip_address or kwargs.get('ip_address')
        final_user_agent = user_agent or kwargs.get('user_agent', '')
        final_source_channel = source_channel or 'WEB_ADMIN'

        if request is not None:
            if not final_request_id:
                final_request_id = getattr(request, 'request_id', '') or request.META.get('HTTP_X_REQUEST_ID', '') or str(uuid.uuid4())
            if not final_correlation_id:
                final_correlation_id = getattr(request, 'correlation_id', '') or request.META.get('HTTP_X_CORRELATION_ID', '') or str(uuid.uuid4())
            if not final_ip_address:
                final_ip_address = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
            if not final_user_agent:
                final_user_agent = request.META.get('HTTP_USER_AGENT', '')
            if not source_channel:
                source_app = request.META.get('HTTP_X_SOURCE_APPLICATION', '').lower()
                if 'mobile' in source_app:
                    final_source_channel = 'MOBILE_APP'
                elif 'member' in source_app:
                    final_source_channel = 'MEMBER_PORTAL'
                elif 'turnstile' in source_app:
                    final_source_channel = 'TURNSTILE_IOT'
                elif 'voice' in source_app:
                    final_source_channel = 'AI_VOICE_AGENT'
        else:
            if not final_request_id:
                final_request_id = str(uuid.uuid4())
            if not final_correlation_id:
                final_correlation_id = str(uuid.uuid4())
            if not source_channel:
                final_source_channel = 'BACKGROUND_WORKER' if res_actor_type == 'SYSTEM' else 'WEB_ADMIN'

        # 8. Create and Persist Canonical ActivityAuditLog Entry
        try:
            entry = ActivityAuditLog.objects.using(target_db).create(
                organization=res_org,
                branch=res_branch,
                branch_name=res_branch.name if res_branch else '',
                actor_type=res_actor_type,
                actor_user=res_actor_user,
                actor_id=res_actor_id,
                actor_email=res_actor_email,
                actor_name=res_actor_name,
                actor_role=res_actor_role,
                action_code=action_code,
                action_category=res_category,
                action_verb=res_verb,
                module=module,
                submodule=submodule,
                entity_type=entity_type,
                entity_id=str(resolved_entity_id) if resolved_entity_id is not None else None,
                entity_name=resolved_entity_name,
                before_state=sanitized_before,
                after_state=sanitized_after,
                field_changes=res_field_changes,
                event_description=final_description,
                reason=reason,
                remarks=remarks,
                result=result,
                error_code=error_code,
                error_message=error_message,
                metadata=sanitize_state(metadata),
                ip_address=final_ip_address,
                user_agent=final_user_agent,
                request_id=str(final_request_id),
                correlation_id=str(final_correlation_id),
                source_channel=final_source_channel,
                occurred_at=timezone.now(),
            )
            mark_request_audited()
            return entry
        except Exception as e:
            logger.error(f"Failed to persist ActivityAuditLog: {e}", exc_info=True)
            return None

    @staticmethod
    def _infer_category(action_code: str, module: str) -> str:
        ac = action_code.upper()
        mod = module.upper()
        if 'AUTH' in ac or 'LOGIN' in ac or 'PASSWORD' in ac or 'MFA' in ac or 'SESSION' in ac:
            return 'AUTHENTICATION'
        if 'MEMBERSHIP' in ac or 'PACKAGE' in ac or 'FREEZE' in ac or 'UNFREEZE' in ac or 'MEMBERS' in mod:
            return 'MEMBERSHIP'
        if 'ORDER' in ac or 'PAYMENT' in ac or 'REFUND' in ac or 'COMMERCE' in mod or 'INVOICE' in ac:
            return 'COMMERCE'
        if 'CLASS' in ac or 'BOOKING' in ac or 'SCHEDULE' in ac or 'ATTENDANCE' in ac or 'OPERATIONS' in mod:
            return 'OPERATIONS'
        if 'ROLE' in ac or 'PERMISSION' in ac or 'ACCESS' in ac or 'TURNSTILE' in ac:
            return 'ACCESS_CONTROL'
        if 'LEAD' in ac or 'INTAKE' in ac or 'CALL' in ac or 'SMS' in ac or 'COMMUNICATION' in mod:
            return 'COMMUNICATION'
        if 'STAFF' in ac or 'TRAINER' in ac or 'EMPLOYEE' in ac or 'ROSTER' in ac:
            return 'WORKFORCE'
        if 'SETTING' in ac or 'CONFIG' in ac or 'POLICY' in ac:
            return 'SETTINGS'
        if 'SYSTEM' in ac or 'JOB' in ac or 'WORKER' in ac or 'SYNC' in ac:
            return 'SYSTEM'
        return 'GENERAL'

    @staticmethod
    def _infer_verb(action_code: str) -> str:
        parts = action_code.split('.')
        last_part = parts[-1].upper() if parts else action_code.upper()
        if 'CREATE' in last_part or 'REGISTER' in last_part or 'NEW' in last_part:
            return 'CREATED'
        if 'UPDATE' in last_part or 'EDIT' in last_part or 'MODIFY' in last_part:
            return 'UPDATED'
        if 'DELETE' in last_part or 'DESTROY' in last_part or 'REMOVE' in last_part:
            return 'DELETED'
        if 'FREEZE' in last_part and 'UN' not in last_part:
            return 'FROZEN'
        if 'UNFREEZE' in last_part:
            return 'UNFROZEN'
        if 'CANCEL' in last_part:
            return 'CANCELLED'
        if 'EXTEND' in last_part:
            return 'EXTENDED'
        if 'TRANSFER' in last_part:
            return 'TRANSFERRED'
        if 'RENEW' in last_part:
            return 'RENEWED'
        if 'UPGRADE' in last_part:
            return 'UPGRADED'
        if 'DOWNGRADE' in last_part:
            return 'DOWNGRADED'
        if 'ASSIGN' in last_part and 'UN' not in last_part:
            return 'ASSIGNED'
        if 'UNASSIGN' in last_part:
            return 'UNASSIGNED'
        if 'PAY' in last_part or 'CAPTURE' in last_part or 'SUCCESS' in last_part:
            return 'PAID'
        if 'REFUND' in last_part:
            return 'REFUNDED'
        if 'LOGIN' in last_part:
            return 'LOGGED_IN'
        if 'LOGOUT' in last_part:
            return 'LOGGED_OUT'
        if 'CHECK_IN' in last_part or 'PUNCH' in last_part:
            return 'CHECKED_IN'
        if 'DENIED' in last_part or 'REJECT' in last_part:
            return 'DENIED'
        if 'APPROVE' in last_part:
            return 'APPROVED'
        return last_part
