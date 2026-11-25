"""
apps/tenant_core/services_media.py — Access rules and signed URLs for uploaded media files.

Replaces the former public ``django.views.static.serve`` route for ``/media/``.
"""
import logging
import os
import posixpath
from typing import Optional
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.core.exceptions import SuspiciousFileOperation
from django.db.models import Q
from django.utils._os import safe_join

from config.routers import get_tenant_db_alias
from .models_assessment import MemberAssessment
from .models_org import Organization

logger = logging.getLogger(__name__)

MEDIA_SIGNING_SALT = 'tenant_core.protected_media'
MEDIA_SIGNATURE_MAX_AGE_SECONDS = 600
ASSESSMENT_MEDIA_PREFIX = 'assessments/'


def build_signed_media_url(file_name: str) -> str:
    """Return a ``/media/`` URL carrying a short-lived signature for ``file_name``."""
    token = signing.dumps(file_name, salt=MEDIA_SIGNING_SALT)
    return f"{settings.MEDIA_URL}{file_name}?sig={quote(token, safe='')}"


def is_valid_media_signature(token: Optional[str], file_name: str) -> bool:
    """True when ``token`` was issued for exactly ``file_name`` and has not expired."""
    if not token:
        return False
    try:
        signed_name = signing.loads(
            token, salt=MEDIA_SIGNING_SALT, max_age=MEDIA_SIGNATURE_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return False
    return signed_name == file_name


def resolve_media_file(relative_path: str) -> Optional[str]:
    """
    Map a request path to an absolute file under MEDIA_ROOT.
    Returns None for non-normalized paths, path traversal, or missing files.
    """
    if not relative_path or posixpath.normpath(relative_path) != relative_path:
        return None
    try:
        full_path = safe_join(settings.MEDIA_ROOT, relative_path)
    except SuspiciousFileOperation:
        return None
    return full_path if os.path.isfile(full_path) else None


def user_can_read_assessment_file(user, file_name: str) -> bool:
    """
    Ownership check for assessment photos, run against the requester's tenant database.
    Staff may read photos of assessments in their organization.
    Members may read photos only of their own APPROVED assessments.
    """
    alias = get_tenant_db_alias() or 'default'
    org = Organization.objects.using(alias).first()
    assessments = MemberAssessment.objects.using(alias).filter(
        Q(photo_front=file_name) | Q(photo_side=file_name),
        organization=org,
    )
    if getattr(user, 'user_type', None) == 'MEMBER':
        assessments = assessments.filter(user_profile__user=user, status='APPROVED')
    return assessments.exists()
