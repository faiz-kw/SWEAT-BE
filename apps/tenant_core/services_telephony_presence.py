"""
apps/tenant_core/services_telephony_presence.py

Presence heartbeat rules. The browser sends a pure liveness ping (no status). The server decides
what the agent's status becomes.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def status_after_heartbeat(presence, *, is_registered: bool, now=None) -> str:
    """
    Returns the status an agent should have after a browser heartbeat.

    - No registered SIP line: OFFLINE.
    - ON_CALL with a live call (session open and younger than the maximum call duration): stays ON_CALL.
    - ON_CALL with a session open longer than the maximum: freed, with a warning (lost hangup).
    - Anything else (OFFLINE, AVAILABLE, WRAP_UP, ON_CALL whose session ended): AVAILABLE.
    """
    if not is_registered:
        return 'OFFLINE'
    now = now or timezone.now()
    session = presence.active_session if presence.status == 'ON_CALL' else None
    if session is None or session.ended_at is not None:
        return 'AVAILABLE'
    started = session.started_at or session.created_at
    cap = timedelta(minutes=int(getattr(settings, 'TELEPHONY_MAX_CALL_DURATION_MINUTES', 120)))
    if now - started <= cap:
        return 'ON_CALL'
    logger.warning(
        "presence heartbeat: session %s open for more than %s; freeing agent %s",
        session.id, cap, presence.user_id,
    )
    return 'AVAILABLE'


def sanitize_tab_id(raw) -> str:
    """Keeps only letters, digits and dashes (max 64) so a client-supplied tab id cannot inject log lines."""
    return ''.join(ch for ch in str(raw or '') if ch.isalnum() or ch == '-')[:64]
