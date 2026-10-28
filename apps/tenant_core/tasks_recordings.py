"""
apps/tenant_core/tasks_recordings.py — Secure Audio Recording Archival Worker.

Celery background task that downloads call recording audio from Vobiz CDN,
archives it locally into `media/recordings/{session_id}.mp3` (or private S3 bucket),
and updates CallSession.recording_storage_key.
Mitigates Risk R6 (Public audio leaks on carrier CDN).
"""

import os
import logging
import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_recordings.archive_vobiz_recording_task',
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def archive_vobiz_recording_task(self, session_id: str, recording_url: str):
    """
    Downloads recording from Vobiz CDN into private storage.
    Updates CallSession.recording_storage_key with the private path.
    """
    if not recording_url or not session_id:
        logger.warning("archive_vobiz_recording_task skipped: missing session_id or recording_url.")
        return {'status': 'SKIPPED', 'reason': 'Missing parameters'}

    try:
        cache_dir = os.path.join(settings.BASE_DIR, 'media', 'recordings')
        os.makedirs(cache_dir, exist_ok=True)
        local_filename = f"{session_id}.mp3"
        local_path = os.path.join(cache_dir, local_filename)

        # Avoid redundant downloads if already cached and non-empty
        if os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
            logger.info("Recording %s already archived at %s", session_id, local_path)
            return {'status': 'ALREADY_ARCHIVED', 'path': local_path}

        # Fetch audio stream from Vobiz CDN
        resp = requests.get(recording_url, timeout=45, stream=True)
        if resp.status_code == 200:
            with open(local_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            # Update CallSession in active tenant context
            from apps.tenant_core.views_telephony import resolve_telephony_tenant_context
            from apps.tenant_core.models_crm import CallSession

            with resolve_telephony_tenant_context():
                CallSession.objects.filter(id=session_id).update(
                    recording_storage_key=local_path,
                    updated_at=timezone.now(),
                )

            logger.info("Archived Vobiz recording for session %s to %s (%d bytes)", session_id, local_path, os.path.getsize(local_path))
            return {'status': 'ARCHIVED', 'path': local_path, 'size_bytes': os.path.getsize(local_path)}
        else:
            logger.warning("Vobiz CDN returned HTTP %s for session %s url=%s", resp.status_code, session_id, recording_url)
            return {'status': 'FAILED', 'http_status': resp.status_code}

    except Exception as exc:
        logger.exception("Error in archive_vobiz_recording_task for session %s: %s", session_id, exc)
        raise self.retry(exc=exc)
