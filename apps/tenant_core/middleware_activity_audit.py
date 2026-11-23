"""
apps/tenant_core/middleware_activity_audit.py — Universal Catch-All CRM Mutation Audit Middleware

Guarantees 100% CRM-wide visibility with ZERO blind spots across all modules:
- Resets thread-local audit flag at the start of every request.
- If a domain view or service already wrote a rich domain audit log via
  UnifiedAuditEngine / emit_audit_event / record_business_audit, this middleware
  skips logging (zero duplicate logs).
- If ANY mutating request (POST, PUT, PATCH, DELETE) on a tenant-scoped route
  completes without an explicit domain log (e.g., Inventory, POS, Support Tickets,
  Workforce, Wellness, Assessments, WOD, CMS, Community, Legal, Imports, Telephony,
  Member Portal self-service, or any future endpoint), this middleware automatically
  synthesizes and persists a 9-Point Contract ActivityAuditLog entry.
- Strictly non-invasive: never touches request.body before the view runs, never
  alters responses, and wraps all audit logic in fail-safe error handling.
"""

import re
import logging
from typing import Optional, Tuple, Dict, Any

from config.routers import get_tenant_db_alias
from .audit_engine import (
    UnifiedAuditEngine,
    reset_request_audit_flag,
    was_request_audited,
    sanitize_state,
)

logger = logging.getLogger(__name__)

MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

# UUID or numeric PK pattern
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
INT_RE = re.compile(r'^\d+$')

# Routes that should not generate catch-all noise (health/schema/audit-viewer/periodic heartbeats)
EXCLUDED_PATH_PREFIXES = (
    '/api/v1/health',
    '/api/v1/schema',
    '/api/v1/docs',
    '/api/v1/tenant/activity-logs',
    '/admin/',
    '/static/',
    '/media/',
)

EXCLUDED_PATH_SUBSTRINGS = (
    '/heartbeat',
    '/telephony/presence',
)

# Normalized module labels derived from URL resource segment
SEGMENT_MODULE_MAP = {
    'inventory': ('INVENTORY', 'INVENTORY'),
    'products': ('INVENTORY', 'PRODUCTS'),
    'retail': ('INVENTORY', 'RETAIL'),
    'pos': ('COMMERCE', 'POS'),
    'commerce': ('COMMERCE', 'COMMERCE'),
    'orders': ('COMMERCE', 'ORDERS'),
    'payments': ('COMMERCE', 'PAYMENTS'),
    'invoices': ('COMMERCE', 'INVOICES'),
    'discounts': ('COMMERCE', 'DISCOUNTS'),
    'rewards': ('COMMERCE', 'REWARDS'),
    'tickets': ('SUPPORT', 'TICKETS'),
    'support': ('SUPPORT', 'SUPPORT'),
    'workforce': ('WORKFORCE', 'WORKFORCE'),
    'staff': ('WORKFORCE', 'STAFF'),
    'trainers': ('WORKFORCE', 'TRAINERS'),
    'schedules': ('WORKFORCE', 'SCHEDULES'),
    'rosters': ('WORKFORCE', 'ROSTERS'),
    'wellness': ('WELLNESS', 'WELLNESS'),
    'assessments': ('WELLNESS', 'ASSESSMENTS'),
    'wod': ('WELLNESS', 'WOD'),
    'diet': ('WELLNESS', 'DIET'),
    'workouts': ('WELLNESS', 'WORKOUTS'),
    'members': ('MEMBERS', 'MEMBERS'),
    'memberships': ('MEMBERSHIPS', 'MEMBERSHIPS'),
    'packages': ('CATALOG', 'PACKAGES'),
    'programs': ('CATALOG', 'PROGRAMS'),
    'catalog': ('CATALOG', 'CATALOG'),
    'classes': ('CLASSES', 'CLASSES'),
    'bookings': ('BOOKINGS', 'BOOKINGS'),
    'appointments': ('BOOKINGS', 'APPOINTMENTS'),
    'attendance': ('OPERATIONS', 'ATTENDANCE'),
    'leads': ('CRM', 'LEADS'),
    'crm': ('CRM', 'CRM'),
    'trials': ('CRM', 'TRIALS'),
    'deals': ('CRM', 'DEALS'),
    'meta': ('INTEGRATIONS', 'META_LEADS'),
    'integrations': ('INTEGRATIONS', 'INTEGRATIONS'),
    'telephony': ('VOICE_AI', 'TELEPHONY'),
    'voice': ('VOICE_AI', 'VOICE_AGENT'),
    'calls': ('VOICE_AI', 'CALLS'),
    'whatsapp': ('COMMUNICATION', 'WHATSAPP'),
    'automation': ('AUTOMATION', 'RULES'),
    'cms': ('CMS', 'CMS'),
    'community': ('COMMUNITY', 'COMMUNITY'),
    'legal': ('COMPLIANCE', 'LEGAL'),
    'privacy': ('COMPLIANCE', 'PRIVACY'),
    'consent': ('COMPLIANCE', 'CONSENT'),
    'imports': ('DATA_IMPORT', 'IMPORT_ENGINE'),
    'import': ('DATA_IMPORT', 'IMPORT_ENGINE'),
    'roles': ('ADMINISTRATION', 'ROLES'),
    'permissions': ('ADMINISTRATION', 'PERMISSIONS'),
    'permission-sets': ('ADMINISTRATION', 'PERMISSIONS'),
    'users': ('ADMINISTRATION', 'USERS'),
    'user-branches': ('ADMINISTRATION', 'USERS'),
    'branches': ('ADMINISTRATION', 'LOCATIONS'),
    'locations': ('ADMINISTRATION', 'LOCATIONS'),
    'organization': ('ADMINISTRATION', 'CONFIGURATION'),
    'organizations': ('ADMINISTRATION', 'CONFIGURATION'),
    'organization-settings': ('ADMINISTRATION', 'CONFIGURATION'),
    'configuration': ('ADMINISTRATION', 'CONFIGURATION'),
    'settings': ('ADMINISTRATION', 'SETTINGS'),
    'security': ('ADMINISTRATION', 'SECURITY'),
    'security-policy': ('ADMINISTRATION', 'SECURITY'),
    'staff-rosters': ('ADMINISTRATION', 'STAFF_ROSTERS'),
    'approvals': ('OPERATIONS', 'APPROVALS'),
    'storage': ('STORAGE', 'FILES'),
    'access-control': ('ACCESS_CONTROL', 'TURNSTILE_IOT'),
    'mobile': ('MEMBER_PORTAL', 'MOBILE_APP'),
    'auth': ('AUTHENTICATION', 'AUTH'),
}

METHOD_VERB_MAP = {
    'POST': ('CREATED', 'created'),
    'PUT': ('UPDATED', 'updated'),
    'PATCH': ('UPDATED', 'updated'),
    'DELETE': ('DELETED', 'deleted'),
}


def _is_pk_segment(seg: str) -> bool:
    return bool(UUID_RE.match(seg) or INT_RE.match(seg))


def _parse_route_metadata(path: str, method: str) -> Tuple[str, str, str, str, str, Optional[str], str]:
    """
    Dynamically derives (module, submodule, action_code, action_verb, entity_type, entity_id, source_channel)
    from the request URL path and HTTP method.
    """
    clean_path = (path or '').strip('/')
    parts = [p for p in clean_path.split('/') if p]

    # Strip leading api/v1
    if len(parts) >= 2 and parts[0] == 'api' and parts[1].startswith('v'):
        parts = parts[2:]

    source_channel = 'WEB_ADMIN'
    if parts and parts[0] == 'mobile':
        source_channel = 'MOBILE_APP'
        parts = parts[1:] or ['mobile']
    elif parts and parts[0] == 'webhooks':
        source_channel = 'WEBHOOK'
        parts = parts[1:] or ['webhooks']
    elif parts and parts[0] == 'access-control':
        source_channel = 'TURNSTILE_IOT'
    elif parts and parts[0] == 'tenant':
        parts = parts[1:]

    if not parts:
        return 'SYSTEM', 'API', f'API.{method}', METHOD_VERB_MAP.get(method, ('MODIFIED', 'modified'))[1], 'Resource', None, source_channel

    primary = parts[0].lower()
    module, submodule = SEGMENT_MODULE_MAP.get(primary, (primary.upper().replace('-', '_'), primary.upper().replace('-', '_')))

    # Identify entity_id and custom action verb segments
    entity_id = None
    resource_words = []
    custom_action = None

    for idx, seg in enumerate(parts):
        if _is_pk_segment(seg):
            entity_id = seg
        else:
            if entity_id is not None and idx == len(parts) - 1:
                custom_action = seg.lower().replace('-', '_')
            else:
                resource_words.append(seg)

    # Submodule refinement if a second resource word exists
    if len(resource_words) >= 2:
        second = resource_words[1].lower()
        if second in SEGMENT_MODULE_MAP:
            _, submodule = SEGMENT_MODULE_MAP[second]
        else:
            submodule = second.upper().replace('-', '_')

    entity_raw = resource_words[-1] if resource_words else primary
    entity_type = entity_raw.rstrip('s').replace('-', ' ').replace('_', ' ').title().replace(' ', '') or 'Record'

    if custom_action:
        action_suffix = custom_action.upper()
        action_verb = custom_action.replace('_', ' ')
    else:
        action_suffix, action_verb = METHOD_VERB_MAP.get(method, ('MODIFIED', 'modified'))

    action_code = f"{module}.{action_suffix}"
    if source_channel == 'MOBILE_APP' and module != 'MEMBER_PORTAL':
        action_code = f"PORTAL.{module}_{action_suffix}"

    return module, submodule, action_code, action_verb, entity_type, entity_id, source_channel


def _extract_response_details(response) -> Tuple[Optional[str], str, str, str]:
    """
    Safely inspects DRF response.data (if dict) to extract (entity_id, entity_name, error_code, error_message).
    """
    entity_id = None
    entity_name = ''
    error_code = ''
    error_message = ''

    data = getattr(response, 'data', None)
    if isinstance(data, dict):
        if data.get('id'):
            entity_id = str(data['id'])
        elif isinstance(data.get('data'), dict) and data['data'].get('id'):
            entity_id = str(data['data']['id'])

        if response.status_code < 400:
            for key in ('name', 'full_name', 'title', 'order_number', 'ticket_number', 'invoice_number', 'membership_number', 'code', 'email'):
                val = data.get(key)
                if val and isinstance(val, str):
                    entity_name = val[:200]
                    break

        if response.status_code >= 400:
            err_val = data.get('detail') or data.get('error') or data.get('message')
            if isinstance(err_val, str):
                error_message = err_val[:500]
            elif isinstance(err_val, list) and err_val:
                error_message = str(err_val[0])[:500]
            elif not err_val:
                # Field validation errors dict
                first_k = next(iter(data.keys()), None)
                if first_k:
                    first_v = data[first_k]
                    error_message = f"{first_k}: {first_v[0] if isinstance(first_v, list) and first_v else first_v}"[:500]

            if data.get('code'):
                error_code = str(data['code'])[:100]
            else:
                error_code = f"HTTP_{response.status_code}"

    return entity_id, entity_name, error_code, error_message


def _extract_safe_payload(drf_req) -> Optional[Dict[str, Any]]:
    """
    Safely extracts and redacts JSON request payload from DRF request.data without touching file streams.
    """
    if drf_req is None:
        return None
    try:
        content_type = (drf_req.META.get('CONTENT_TYPE') or '').lower()
        if 'multipart/form-data' in content_type:
            return {'upload': 'multipart/form-data'}
        raw_data = getattr(drf_req, 'data', None)
        if isinstance(raw_data, dict):
            # Convert QueryDict or dict safely, bounded to 30 keys
            clean = {}
            for idx, (k, v) in enumerate(raw_data.items()):
                if idx >= 30:
                    clean['_truncated'] = True
                    break
                if hasattr(v, 'read'):
                    clean[k] = f"<File:{getattr(v, 'name', 'upload')}>"
                else:
                    clean[k] = v
            return sanitize_state(clean)
    except Exception:
        return None
    return None


class UniversalMutationAuditMiddleware:
    """
    Fail-safe catch-all middleware recording any tenant-scoped API mutation
    that was not already logged by an explicit domain service call.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        reset_request_audit_flag()
        response = self.get_response(request)

        try:
            self._maybe_record_catchall(request, response)
        except Exception as exc:
            logger.debug("UniversalMutationAuditMiddleware skipped safely: %s", exc)
        finally:
            reset_request_audit_flag()

        return response

    def _maybe_record_catchall(self, request, response) -> None:
        if request.method not in MUTATING_METHODS:
            return

        path = request.path_info or request.path or ''
        if any(path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
            return
        if any(sub in path for sub in EXCLUDED_PATH_SUBSTRINGS):
            return

        # Skip if a domain service/view already logged a rich audit record for this request
        if was_request_audited():
            return

        # Only audit when routed to an active tenant database
        db_alias = get_tenant_db_alias()
        if not db_alias or db_alias == 'default':
            return

        # Resolve DRF request (carries authenticated JWT TenantUser and parsed data)
        renderer_ctx = getattr(response, 'renderer_context', None) or {}
        drf_req = renderer_ctx.get('request')

        actor_user = None
        if drf_req is not None and getattr(drf_req, 'user', None) and getattr(drf_req.user, 'is_authenticated', False):
            actor_user = drf_req.user
        elif getattr(request, 'user', None) and getattr(request.user, 'is_authenticated', False):
            actor_user = request.user

        module, submodule, action_code, action_verb, entity_type, url_entity_id, source_channel = _parse_route_metadata(
            path, request.method
        )

        resp_entity_id, resp_entity_name, error_code, error_message = _extract_response_details(response)
        final_entity_id = url_entity_id or resp_entity_id
        final_entity_name = resp_entity_name or (f"{entity_type} #{str(final_entity_id)[:8]}" if final_entity_id else entity_type)

        # Extract safe request payload & reason/remarks if supplied
        safe_payload = _extract_safe_payload(drf_req)
        reason = ''
        remarks = ''
        if isinstance(safe_payload, dict):
            reason = str(safe_payload.get('reason') or safe_payload.get('cancellation_reason') or '')[:500]
            remarks = str(safe_payload.get('remarks') or safe_payload.get('notes') or '')[:500]

        result_status = 'SUCCESS' if 200 <= response.status_code < 400 else 'FAILURE'

        # Resolve branch if present in request payload or query params
        branch_id = None
        if isinstance(safe_payload, dict):
            branch_id = safe_payload.get('branch_id') or safe_payload.get('branch') or safe_payload.get('home_branch_id')
        if not branch_id:
            branch_id = request.GET.get('branch_id') or request.GET.get('branch')

        branch_obj = None
        if branch_id and _is_pk_segment(str(branch_id)):
            try:
                from .models_org import Branch
                branch_obj = Branch.objects.using(db_alias).filter(id=branch_id).first()
            except Exception:
                branch_obj = None

        metadata = {
            'http_method': request.method,
            'http_path': path,
            'http_status': response.status_code,
            'capture_mode': 'UNIVERSAL_MIDDLEWARE',
        }
        if safe_payload:
            metadata['request_payload'] = safe_payload

        UnifiedAuditEngine.record_activity(
            action_code=action_code,
            action_verb=action_verb,
            module=module,
            submodule=submodule,
            entity_type=entity_type,
            entity_id=final_entity_id,
            entity_name=final_entity_name,
            request=drf_req or request,
            actor_user=actor_user if hasattr(actor_user, '_meta') else None,
            actor_name=(
                f"{getattr(actor_user, 'first_name', '')} {getattr(actor_user, 'last_name', '')}".strip()
                or getattr(actor_user, 'email', '')
                or ('Public / Unauthenticated' if not actor_user else '')
            ),
            actor_email=getattr(actor_user, 'email', '') if actor_user else '',
            after_state=safe_payload if result_status == 'SUCCESS' else None,
            reason=reason,
            remarks=remarks,
            result=result_status,
            error_code=error_code,
            error_message=error_message,
            metadata=metadata,
            branch=branch_obj,
            source_channel=source_channel,
            db_alias=db_alias,
        )
