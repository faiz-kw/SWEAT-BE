"""
apps/tenant_core/meta_logging.py - Production-safe structured logging for Meta Lead Ads.

Guarantees:
- Structured event code for all integration lifecycle phases
- Explicit contextual fields (correlation_id, import_id, tenant_id, page_id, form_id, leadgen_id, status)
- Strict redaction: NEVER logs access tokens, app secrets, JWTs, authorization headers, or customer PII
- Compatible with stdout/stderr, systemd/journald, Datadog, AWS CloudWatch, and ELK
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger('apps.tenant_core.meta')

# Canonical Meta Event Codes
EVT_WEBHOOK_RECEIVED = 'META_WEBHOOK_RECEIVED'
EVT_SIGNATURE_FAILED = 'META_SIGNATURE_FAILED'
EVT_PAGE_RESOLVED = 'META_PAGE_RESOLVED'
EVT_PAGE_RESOLUTION_FAILED = 'META_PAGE_RESOLUTION_FAILED'
EVT_IMPORT_QUEUED = 'META_IMPORT_QUEUED'
EVT_PROCESSING_STARTED = 'META_PROCESSING_STARTED'
EVT_GRAPH_FETCH_SUCCESS = 'META_GRAPH_FETCH_SUCCESS'
EVT_GRAPH_FETCH_FAILED = 'META_GRAPH_FETCH_FAILED'
EVT_MAPPING_FOUND = 'META_MAPPING_FOUND'
EVT_MAPPING_MISSING = 'META_MAPPING_MISSING'
EVT_LEAD_CREATED = 'META_LEAD_CREATED'
EVT_DUPLICATE_IGNORED = 'META_DUPLICATE_IGNORED'
EVT_RETRY_SCHEDULED = 'META_RETRY_SCHEDULED'
EVT_PROCESSING_FAILED = 'META_PROCESSING_FAILED'
EVT_DEAD_LETTER = 'META_DEAD_LETTER'
EVT_MANUAL_RETRY_TRIGGERED = 'META_MANUAL_RETRY_TRIGGERED'

# Sensitive keys that must NEVER be logged
FORBIDDEN_KEYS = {
    'access_token', 'token', 'secret', 'app_secret', 'authorization',
    'jwt', 'code', 'oauth_code', 'signature', 'state', 'phone', 'email',
    'phone_number', 'first_name', 'last_name', 'full_name'
}


def log_meta_event(
    event_code: str,
    message: str,
    correlation_id: Optional[str] = None,
    import_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    page_id: Optional[str] = None,
    form_id: Optional[str] = None,
    leadgen_id: Optional[str] = None,
    status: Optional[str] = None,
    error_code: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    level: int = logging.INFO,
) -> None:
    """
    Emits a structured log entry with sanitized context.
    """
    details = []
    if correlation_id:
        details.append(f"req={correlation_id}")
    if import_id:
        details.append(f"import={import_id}")
    if tenant_id:
        details.append(f"tenant={tenant_id}")
    if page_id:
        details.append(f"page={page_id}")
    if form_id:
        details.append(f"form={form_id}")
    if leadgen_id:
        details.append(f"leadgen={leadgen_id}")
    if status:
        details.append(f"status={status}")
    if error_code:
        details.append(f"error={error_code}")

    if extra:
        for k, v in extra.items():
            k_lower = str(k).lower()
            if any(forbidden in k_lower for forbidden in FORBIDDEN_KEYS):
                continue
            # Sanitize string representation
            val_str = str(v)
            if 'access_token' in val_str:
                val_str = '[REDACTED]'
            details.append(f"{k}={val_str[:120]}")

    formatted_context = " | ".join(details)
    log_line = f"[{event_code}] {message}"
    if formatted_context:
        log_line = f"{log_line} | {formatted_context}"

    logger.log(level, log_line)
