"""
Workout of the Day (WOD) Phase 1 — Services
Provides:
  1. WODRBACService: Seeds WOD permissions and grants them ONLY to ORG_ADMIN by default
  2. WODTagService: Seeds initial configurable tag groups & values and normalizes tags
  3. WODClassificationService: Enforces content_kind, classification_status, and is_wod_eligible rules
  4. WODVideoService: Handles direct video file upload AND automatic video download from links (Google Drive / External)
  5. WODImportService: Excel (.xlsx / .csv) preview and confirm import pipeline
"""

import csv
import hashlib
import io
import logging
import mimetypes
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models_infra import File
from .models_org import Organization
from .models_rbac import (
    ModuleCatalog,
    Permission,
    Role,
    RoleModuleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleSubmoduleAccess,
    SubmoduleCatalog,
)
from .models_users import TenantUser
from .models_wod import WorkoutContentItem, WorkoutContentTag, WorkoutTag
from .services_reliability import record_business_audit

logger = logging.getLogger(__name__)


# ===========================================================================
# CONSTANTS & INITIAL SEED DEFINITIONS
# ===========================================================================

WOD_PERMISSIONS_SPEC = [
    ('WOD_CONTENT_LIBRARY_VIEW', 'view', 'View WOD Content Library', 'Can view workout movements, videos, and tags in the WOD Content Library'),
    ('WOD_CONTENT_LIBRARY_CREATE', 'create', 'Create WOD Content Items', 'Can create new workout movements and setup videos in the WOD Content Library'),
    ('WOD_CONTENT_LIBRARY_EDIT', 'edit', 'Edit WOD Content Items', 'Can edit workout movements, videos, and tags in the WOD Content Library'),
    ('WOD_CONTENT_LIBRARY_ACTIVATE', 'activate', 'Activate/Deactivate & Set WOD Eligibility', 'Can activate/deactivate workout content and mark items as WOD eligible'),
    ('WOD_CONTENT_LIBRARY_IMPORT', 'import', 'Import WOD Content from Excel', 'Can preview and import Excel workout content into the WOD Content Library'),
    ('WOD_TAG_MANAGE', 'manage_tags', 'Manage WOD Configurable Tags', 'Can create, edit, and activate/deactivate configurable workout tags'),
]

INITIAL_WOD_TAGS: Dict[str, List[str]] = {
    'PROGRAM': [
        'Sweat Stretch',
        'Sweat Total',
        'Sweat Athletic',
    ],
    'SECTION': [
        'Warm Up',
        'Main Zone',
        'Cool Down',
        'Stretch',
        'Core',
    ],
    'USER_LEVEL': [
        'Beginner',
        'Intermediate',
        'Advanced',
    ],
    'INTENSITY': [
        'Low',
        'Moderate',
        'High',
    ],
    'MUSCLE_GROUP': [
        'Full Body',
        'Lower Body',
        'Upper Body',
        'Core',
        'Hip Flexor',
        'Hamstring',
        'Glute',
        'Shoulder',
        'Spine',
        'Chest',
        'Back',
        'Quads',
        'Calves',
        'Arms',
        'Obliques',
    ],
    'BODY_TARGET': [
        'Spine Mobility',
        'Balance',
        'Flexibility',
        'Stability',
        'Posture',
    ],
    'BREATHING': [
        'Exhale - Lift',
        'Exhale - Natural',
        'Exhale - Elbow Extension',
        'Exhale - Knee Extension',
        'Exhale - Pull',
        'Exhale - Heel Drop',
        'Exhale - Twist',
        'Exhale - Move Back',
        'Exhale - Push',
        'Exhale - Tap',
        'Exhale - Leg Lift',
        'Exhale - Spine Lift',
        'Exhale - Jump',
    ],
    'EQUIPMENT': [
        'Bodyweight',
        'Dumbbell',
        'Loop Band',
        'Ankle Weight',
        'Reformer',
        'TRX',
        'Box',
        'Footbar',
        'Mat',
        'Resistance Band',
    ],
    'MOVEMENT_FAMILY': [
        'Squat',
        'Hinge',
        'Lunge',
        'Push',
        'Pull',
        'Plank',
        'Rotation',
        'Mobility',
        'Stretch',
    ],
}

# Synonyms / typo normalization map during Excel import
SECTION_SYNONYMS = {
    'warmup': 'Warm Up',
    'warm-up': 'Warm Up',
    'warm up': 'Warm Up',
    'main': 'Main Zone',
    'main zone': 'Main Zone',
    'mainzone': 'Main Zone',
    'main workout': 'Main Zone',
    'cooldown': 'Cool Down',
    'cool-down': 'Cool Down',
    'cool down': 'Cool Down',
    'stretch': 'Stretch',
    'stretching': 'Stretch',
    'core': 'Core',
    'abs': 'Core',
}

INTENSITY_SYNONYMS = {
    'low': 'Low',
    'light': 'Low',
    'easy': 'Low',
    'moderate': 'Moderate',
    'modrate': 'Moderate',
    'medium': 'Moderate',
    'med': 'Moderate',
    'mid': 'Moderate',
    'high': 'High',
    'hard': 'High',
    'intense': 'High',
}

USER_LEVEL_SYNONYMS = {
    'beginner': 'Beginner',
    'beg': 'Beginner',
    'level 1': 'Beginner',
    'intermediate': 'Intermediate',
    'inter': 'Intermediate',
    'int': 'Intermediate',
    'level 2': 'Intermediate',
    'advanced': 'Advanced',
    'adv': 'Advanced',
    'level 3': 'Advanced',
}

BODY_TARGET_CANONICAL = {
    'spine mobility': 'Spine Mobility',
    'balance': 'Balance',
    'flexibility': 'Flexibility',
    'stability': 'Stability',
    'posture': 'Posture',
}


def slugify_tag_code(name: str) -> str:
    """Convert a human-readable tag name into an uppercase snake_case code."""
    cleaned = re.sub(r'[^a-zA-Z0-9]+', '_', (name or '').strip()).strip('_').upper()
    return cleaned[:100] or 'TAG'


# ===========================================================================
# 1. RBAC SEEDING & AUTHORIZATION HELPER
# ===========================================================================

class WODRBACService:
    """
    Ensures WOD Content Library permissions exist in the tenant DB and are granted
    ONLY to the tenant's ORG_ADMIN role initially.
    Designed so additional roles can be granted these permissions later via standard RBAC
    without changing the WOD module code.
    """

    @classmethod
    def ensure_wod_permissions_seeded(cls, organization: Organization, db_alias: str = 'default') -> None:
        if not organization:
            return
        try:
            # Find or create a host ModuleCatalog in the tenant DB (prefer 'ops' so it aligns with enabled modules)
            module = (
                ModuleCatalog.objects.using(db_alias).filter(module_code__iexact='ops').first()
                or ModuleCatalog.objects.using(db_alias).filter(code__iexact='ops').first()
                or ModuleCatalog.objects.using(db_alias).filter(is_enabled=True).first()
            )
            if not module:
                module = ModuleCatalog.objects.using(db_alias).create(
                    code='ops',
                    module_code='ops',
                    source_module_id=uuid.uuid4(),
                    name='Studio Operations & Scheduling',
                    is_enabled=True,
                    status='ACTIVE',
                )

            submodule = (
                SubmoduleCatalog.objects.using(db_alias)
                .filter(module=module, submodule_code__iexact='wod_content_library')
                .first()
            )
            if not submodule:
                submodule = SubmoduleCatalog.objects.using(db_alias).create(
                    module=module,
                    code='wod_content_library',
                    submodule_code='wod_content_library',
                    name='WOD Content Library',
                    description='Admin-only Workout of the Day Content Library & Tag Master',
                    is_enabled=True,
                    status='ACTIVE',
                )

            created_Or_found_perms: List[Permission] = []
            for perm_code, action, label, desc in WOD_PERMISSIONS_SPEC:
                perm = (
                    Permission.objects.using(db_alias).filter(permission_code__iexact=perm_code).first()
                    or Permission.objects.using(db_alias).filter(code__iexact=perm_code).first()
                )
                if not perm:
                    perm = Permission(
                        source_permission_id=uuid.uuid4(),
                        module=module,
                        submodule=submodule,
                        code=perm_code,
                        permission_code=perm_code,
                        action=action,
                        label=label,
                        description=desc,
                        status='ACTIVE',
                        is_active=True,
                    )
                    perm.save(using=db_alias)
                created_Or_found_perms.append(perm)

            # Grant these permissions ONLY to ORG_ADMIN role by default (if not already configured)
            admin_roles = list(
                Role.objects.using(db_alias).filter(
                    organization=organization,
                    code__in=['ORG_ADMIN', 'TENANT_ADMIN', 'ADMIN'],
                    is_active=True,
                )
            )
            for admin_role in admin_roles:
                # Ensure RoleModuleAccess & RoleSubmoduleAccess for the host module/submodule
                RoleModuleAccess.objects.using(db_alias).get_or_create(
                    role=admin_role,
                    module=module,
                    defaults={'can_access': True},
                )
                RoleSubmoduleAccess.objects.using(db_alias).get_or_create(
                    role=admin_role,
                    submodule=submodule,
                    defaults={'can_access': True},
                )
                perm_set = (
                    RolePermissionSet.objects.using(db_alias)
                    .filter(role=admin_role, is_active=True)
                    .first()
                )
                if not perm_set:
                    perm_set = RolePermissionSet.objects.using(db_alias).create(
                        role=admin_role,
                        organization=organization,
                        scope_type='ORGANIZATION',
                        name=f"{admin_role.name} Default Permission Set",
                        status='ACTIVE',
                        is_active=True,
                    )
                for perm in created_Or_found_perms:
                    exists = RolePermissionSetItem.objects.using(db_alias).filter(
                        permission_set=perm_set,
                        permission=perm,
                    ).exists()
                    if not exists:
                        RolePermissionSetItem.objects.using(db_alias).create(
                            permission_set=perm_set,
                            permission=perm,
                            granted=True,
                        )
        except Exception as exc:
            logger.warning("WODRBACService.ensure_wod_permissions_seeded warning: %s", exc)


# ===========================================================================
# 2. TAG SEEDING & NORMALIZATION SERVICE
# ===========================================================================

class WODTagService:
    """
    Manages configurable workout tags (`workout_tags`) and normalizes tag strings.
    """

    @classmethod
    def ensure_initial_tags(cls, organization: Organization, db_alias: str = 'default') -> None:
        if not organization:
            return
        existing_pairs = set(
            WorkoutTag.objects.using(db_alias)
            .filter(organization=organization)
            .values_list('tag_group', 'code')
        )
        to_create = []
        for group, names in INITIAL_WOD_TAGS.items():
            for idx, name in enumerate(names, start=1):
                code = slugify_tag_code(name)
                if (group, code) not in existing_pairs:
                    to_create.append(
                        WorkoutTag(
                            organization=organization,
                            tag_group=group,
                            code=code,
                            name=name,
                            display_order=idx * 10,
                            status='ACTIVE',
                        )
                    )
                    existing_pairs.add((group, code))
        if to_create:
            WorkoutTag.objects.using(db_alias).bulk_create(to_create, ignore_conflicts=True)

    @classmethod
    def get_or_create_tag(
        cls,
        organization: Organization,
        tag_group: str,
        name: str,
        db_alias: str = 'default',
    ) -> Optional[WorkoutTag]:
        clean_name = (name or '').strip()
        if not clean_name:
            return None
        group = tag_group.strip().upper()
        code = slugify_tag_code(clean_name)
        tag = (
            WorkoutTag.objects.using(db_alias)
            .filter(organization=organization, tag_group=group, code=code)
            .first()
        )
        if not tag:
            max_order = (
                WorkoutTag.objects.using(db_alias)
                .filter(organization=organization, tag_group=group)
                .count()
            )
            tag = WorkoutTag.objects.using(db_alias).create(
                organization=organization,
                tag_group=group,
                code=code,
                name=clean_name,
                display_order=(max_order + 1) * 10,
                status='ACTIVE',
            )
        return tag


# ===========================================================================
# 3. CLASSIFICATION & WOD ELIGIBILITY ENGINE
# ===========================================================================

class WODClassificationService:
    """
    Evaluates classification completeness and enforces WOD eligibility rules.
    """

    @classmethod
    def detect_content_kind(cls, movement_name: str, explicit_kind: Optional[str] = None) -> str:
        if explicit_kind and explicit_kind.upper() in ('MOVEMENT', 'SETUP', 'INSTRUCTION', 'TECHNIQUE', 'OTHER'):
            # Still auto-detect setup/instruction if movement_name clearly begins with 'How to '
            lower_name = (movement_name or '').strip().lower()
            if lower_name.startswith('how to set up') or lower_name.startswith('how to setup') or lower_name.startswith('how to wear'):
                if explicit_kind.upper() == 'MOVEMENT':
                    return 'SETUP'
            return explicit_kind.upper()

        lower_name = (movement_name or '').strip().lower()
        if (
            lower_name.startswith('how to set up')
            or lower_name.startswith('how to setup')
            or lower_name.startswith('how to wear')
            or 'setup ' in lower_name
            or 'set up ' in lower_name
        ):
            return 'SETUP'
        if lower_name.startswith('how to ') or 'instruction' in lower_name:
            return 'INSTRUCTION'
        return 'MOVEMENT'

    @classmethod
    def evaluate_item(
        cls,
        item: WorkoutContentItem,
        db_alias: str = 'default',
        save: bool = True,
    ) -> Dict[str, Any]:
        """
        Inspects the item's fields, video status, and attached active tags.
        Computes:
          - classification_status ('COMPLETE', 'INCOMPLETE', 'NEEDS_REVIEW')
          - is_wod_eligible (enforces False if requirements are not met)
          - missing_requirements (list of human-readable strings)
        """
        attached_tags = list(
            WorkoutContentTag.objects.using(db_alias)
            .filter(content_item=item, tag__status='ACTIVE')
            .select_related('tag')
        )
        groups_present = {ct.tag.tag_group for ct in attached_tags if ct.tag}

        missing: List[str] = []
        has_name = bool(item.movement_name and item.movement_name.strip())
        if not has_name:
            missing.append('Movement name is required')

        has_program = 'PROGRAM' in groups_present
        has_section = 'SECTION' in groups_present
        has_intensity = 'INTENSITY' in groups_present
        has_muscle = bool({'MUSCLE_GROUP', 'BODY_TARGET'} & groups_present)

        # Video check: either a downloaded/uploaded file OR a valid video_url that hasn't failed download
        video_failed = item.video_download_status == 'FAILED'
        has_valid_video = bool(
            item.has_stored_video
            or (item.video_url and item.video_url.strip() and not video_failed)
        )

        if video_failed:
            missing.append(f"Video download failed ({item.video_download_error or 'broken or restricted link'})")
        elif not has_valid_video:
            missing.append('Video attachment or valid video link is required')

        if item.content_kind == 'MOVEMENT':
            if not has_program:
                missing.append('At least 1 PROGRAM tag is required')
            if not has_section:
                missing.append('At least 1 SECTION tag is required')
            if not has_intensity:
                missing.append('At least 1 INTENSITY tag is required')
            if not has_muscle:
                missing.append('At least 1 MUSCLE_GROUP or BODY_TARGET tag is required')

            if video_failed:
                new_classification = 'NEEDS_REVIEW'
            elif not missing:
                new_classification = 'COMPLETE'
            else:
                new_classification = 'NEEDS_REVIEW' if item.classification_status == 'NEEDS_REVIEW' else 'INCOMPLETE'
        else:
            # SETUP / INSTRUCTION / TECHNIQUE / OTHER
            if video_failed:
                new_classification = 'NEEDS_REVIEW'
            elif has_name and has_valid_video:
                new_classification = 'COMPLETE'
            else:
                new_classification = 'NEEDS_REVIEW' if item.classification_status == 'NEEDS_REVIEW' else 'INCOMPLETE'

        # Enforce WOD eligibility rules:
        # Setup/Instruction/Technique/Other are NEVER WOD eligible.
        # Movements can only be WOD eligible when ACTIVE, COMPLETE, and has_valid_video.
        can_be_eligible = (
            item.content_kind == 'MOVEMENT'
            and item.status == 'ACTIVE'
            and new_classification == 'COMPLETE'
            and has_valid_video
        )
        new_eligible = bool(item.is_wod_eligible and can_be_eligible)

        changed_fields = []
        if item.classification_status != new_classification:
            item.classification_status = new_classification
            changed_fields.append('classification_status')
        if item.is_wod_eligible != new_eligible:
            item.is_wod_eligible = new_eligible
            changed_fields.append('is_wod_eligible')

        if save and changed_fields:
            changed_fields.append('updated_at')
            item.save(using=db_alias, update_fields=changed_fields)

        return {
            'classification_status': item.classification_status,
            'is_wod_eligible': item.is_wod_eligible,
            'can_be_wod_eligible': can_be_eligible,
            'missing_requirements': missing,
            'groups_present': sorted(list(groups_present)),
        }

    @classmethod
    def set_wod_eligibility(
        cls,
        item: WorkoutContentItem,
        eligible: bool,
        db_alias: str = 'default',
    ) -> WorkoutContentItem:
        eval_res = cls.evaluate_item(item, db_alias=db_alias, save=False)
        if eligible:
            if item.content_kind != 'MOVEMENT':
                raise ValidationError(
                    f"Content item '{item.movement_name}' is of kind '{item.content_kind}'. "
                    "Only MOVEMENT items can be marked as WOD eligible."
                )
            if item.status != 'ACTIVE':
                raise ValidationError(
                    f"Content item '{item.movement_name}' must be ACTIVE before marking as WOD eligible (current status: {item.status})."
                )
            if eval_res['missing_requirements']:
                raise ValidationError(
                    "Cannot mark movement as WOD eligible because classification is incomplete: "
                    + "; ".join(eval_res['missing_requirements'])
                )
            item.is_wod_eligible = True
        else:
            item.is_wod_eligible = False

        item.save(using=db_alias, update_fields=['classification_status', 'is_wod_eligible', 'updated_at'])
        return item


# ===========================================================================
# 4. VIDEO UPLOAD & AUTOMATIC LINK DOWNLOAD SERVICE
# ===========================================================================

class WODVideoService:
    """
    Handles storing workout videos from either:
      1. Direct file upload (multipart/form-data)
      2. Automatic download from a provided video link (Google Drive / direct URL / external URL)
    All stored videos are saved inside `<BASE_DIR>/media/wod_videos/<org_id>/` and registered
    in the tenant `File` table for full traceability and instant HTML5 streaming.
    """

    GOOGLE_DRIVE_FILE_PATTERNS = [
        re.compile(r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)'),
        re.compile(r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)'),
        re.compile(r'drive\.google\.com/uc\?.*id=([a-zA-Z0-9_-]+)'),
        re.compile(r'docs\.google\.com/file/d/([a-zA-Z0-9_-]+)'),
    ]

    @classmethod
    def get_media_dir(cls, org_id: Any) -> Path:
        base_media = getattr(settings, 'MEDIA_ROOT', None) or (Path(settings.BASE_DIR) / 'media')
        target_dir = Path(base_media) / 'wod_videos' / str(org_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    @classmethod
    def detect_provider_from_url(cls, url: Optional[str]) -> Optional[str]:
        if not url or not url.strip():
            return None
        lower = url.strip().lower()
        if 'drive.google.com' in lower or 'docs.google.com' in lower:
            return 'GOOGLE_DRIVE'
        if 'youtube.com' in lower or 'youtu.be' in lower:
            return 'YOUTUBE'
        if 'zata.ai' in lower:
            return 'ZATA'
        return 'EXTERNAL'

    @classmethod
    def extract_google_drive_file_id(cls, url: str) -> Optional[str]:
        for pattern in cls.GOOGLE_DRIVE_FILE_PATTERNS:
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _register_file_record(
        cls,
        item: WorkoutContentItem,
        rel_path: str,
        file_name: str,
        file_size: int,
        mime_type: str,
        checksum: str,
        actor: Optional[TenantUser] = None,
        db_alias: str = 'default',
    ) -> File:
        object_key = f"tenants/{item.organization_id}/wod_videos/{item.id}/{uuid.uuid4().hex[:8]}_{file_name}"
        file_rec = File.objects.using(db_alias).create(
            owner_type='WorkoutContentItem',
            owner_id=item.id,
            entity_type='WorkoutContentItem',
            entity_id=item.id,
            file_name=file_name[:255],
            original_file_name=file_name[:255],
            original_filename=file_name[:255],
            storage_provider='LOCAL_MEDIA',
            bucket_reference='wod-media-storage',
            bucket_name='wod-media-storage',
            object_key=object_key,
            mime_type=mime_type or 'video/mp4',
            file_size=file_size,
            checksum=checksum,
            classification='INTERNAL',
            uploaded_by=actor if (actor and hasattr(actor, 'pk') and getattr(actor, '_auth_type', None) == 'tenant') else None,
            uploaded_at=timezone.now(),
            is_deleted=False,
        )
        return file_rec

    @classmethod
    def save_uploaded_video(
        cls,
        item: WorkoutContentItem,
        uploaded_file,
        actor: Optional[TenantUser] = None,
        db_alias: str = 'default',
    ) -> WorkoutContentItem:
        """
        Saves an uploaded video file directly to disk + tenant File table and marks video COMPLETED.
        """
        orig_name = getattr(uploaded_file, 'name', None) or f"{slugify_tag_code(item.movement_name).lower()}.mp4"
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(orig_name))
        if not any(safe_name.lower().endswith(ext) for ext in ('.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv')):
            safe_name = f"{safe_name}.mp4"

        media_dir = cls.get_media_dir(item.organization_id)
        stored_filename = f"{item.id}_{safe_name}"
        full_path = media_dir / stored_filename

        sha256 = hashlib.sha256()
        total_bytes = 0
        with open(full_path, 'wb') as out_f:
            if hasattr(uploaded_file, 'chunks'):
                for chunk in uploaded_file.chunks():
                    out_f.write(chunk)
                    sha256.update(chunk)
                    total_bytes += len(chunk)
            else:
                data = uploaded_file.read()
                if isinstance(data, str):
                    data = data.encode('utf-8')
                out_f.write(data)
                sha256.update(data)
                total_bytes += len(data)

        mime_type = (
            getattr(uploaded_file, 'content_type', None)
            or mimetypes.guess_type(safe_name)[0]
            or 'video/mp4'
        )
        rel_path = f"wod_videos/{item.organization_id}/{stored_filename}"

        file_rec = cls._register_file_record(
            item=item,
            rel_path=rel_path,
            file_name=safe_name,
            file_size=total_bytes,
            mime_type=mime_type,
            checksum=sha256.hexdigest(),
            actor=actor,
            db_alias=db_alias,
        )

        item.video_file = file_rec
        item.video_storage_path = rel_path
        item.video_file_name = safe_name
        item.video_file_size = total_bytes
        item.video_mime_type = mime_type
        item.video_provider = item.video_provider or 'UPLOAD'
        item.video_download_status = 'COMPLETED'
        item.video_download_error = None
        item.video_downloaded_at = timezone.now()
        item.save(
            using=db_alias,
            update_fields=[
                'video_file',
                'video_storage_path',
                'video_file_name',
                'video_file_size',
                'video_mime_type',
                'video_provider',
                'video_download_status',
                'video_download_error',
                'video_downloaded_at',
                'updated_at',
            ],
        )
        WODClassificationService.evaluate_item(item, db_alias=db_alias, save=True)
        return item

    @classmethod
    def download_video_from_url(
        cls,
        item: WorkoutContentItem,
        url: Optional[str] = None,
        actor: Optional[TenantUser] = None,
        db_alias: str = 'default',
        timeout: int = 25,
    ) -> WorkoutContentItem:
        """
        Downloads the video file from `url` (or `item.video_url`), resolving Google Drive sharing
        links to direct download streams, and saves the downloaded binary into managed storage.
        If the link is invalid, restricted, or a folder link, records `video_download_status = 'FAILED'`
        with a descriptive `video_download_error` and marks `classification_status = 'NEEDS_REVIEW'`.
        """
        target_url = (url if url is not None else item.video_url or '').strip()
        if not target_url:
            item.video_download_status = 'NONE'
            item.video_download_error = None
            item.save(using=db_alias, update_fields=['video_download_status', 'video_download_error', 'updated_at'])
            WODClassificationService.evaluate_item(item, db_alias=db_alias, save=True)
            return item

        item.video_url = target_url
        if not item.video_provider or item.video_provider == 'UPLOAD':
            item.video_provider = cls.detect_provider_from_url(target_url)

        # Reject obvious non-URLs or folder URLs early
        if not (target_url.startswith('http://') or target_url.startswith('https://')):
            item.video_download_status = 'FAILED'
            item.video_download_error = 'Invalid video URL format (must start with http:// or https://).'
            item.classification_status = 'NEEDS_REVIEW'
            item.is_wod_eligible = False
            item.save(
                using=db_alias,
                update_fields=[
                    'video_url',
                    'video_provider',
                    'video_download_status',
                    'video_download_error',
                    'classification_status',
                    'is_wod_eligible',
                    'updated_at',
                ],
            )
            return item

        lower_url = target_url.lower()
        if 'drive.google.com/drive/folders/' in lower_url or 'drive.google.com/drive/u/' in lower_url and '/folders/' in lower_url:
            item.video_download_status = 'FAILED'
            item.video_download_error = 'Google Drive link points to a folder instead of an individual video file.'
            item.classification_status = 'NEEDS_REVIEW'
            item.is_wod_eligible = False
            item.save(
                using=db_alias,
                update_fields=[
                    'video_url',
                    'video_provider',
                    'video_download_status',
                    'video_download_error',
                    'classification_status',
                    'is_wod_eligible',
                    'updated_at',
                ],
            )
            return item

        item.video_download_status = 'DOWNLOADING'
        item.video_download_error = None
        item.save(using=db_alias, update_fields=['video_url', 'video_provider', 'video_download_status', 'video_download_error', 'updated_at'])

        try:
            session = requests.Session()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            }

            gdrive_id = cls.extract_google_drive_file_id(target_url)
            if gdrive_id:
                download_url = f"https://drive.google.com/uc?export=download&id={gdrive_id}"
                resp = session.get(download_url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
                content_type = (resp.headers.get('Content-Type') or '').lower()

                # Handle Google Drive virus scan warning page for larger video files
                if 'text/html' in content_type and resp.status_code == 200:
                    confirm_token = None
                    for cookie_name, cookie_val in resp.cookies.items():
                        if cookie_name.startswith('download_warning'):
                            confirm_token = cookie_val
                            break
                    if not confirm_token:
                        # Try confirm=t parameter on drive.usercontent.google.com or drive.google.com
                        download_url_confirm = f"https://drive.usercontent.google.com/download?id={gdrive_id}&export=download&confirm=t"
                        resp.close()
                        resp = session.get(download_url_confirm, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
                        content_type = (resp.headers.get('Content-Type') or '').lower()
                    else:
                        download_url_confirm = f"https://drive.google.com/uc?export=download&id={gdrive_id}&confirm={confirm_token}"
                        resp.close()
                        resp = session.get(download_url_confirm, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
                        content_type = (resp.headers.get('Content-Type') or '').lower()
            else:
                resp = session.get(target_url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
                content_type = (resp.headers.get('Content-Type') or '').lower()

            if resp.status_code != 200:
                resp.close()
                raise ValueError(f"HTTP {resp.status_code} returned when attempting to download video from link.")

            if 'text/html' in content_type:
                resp.close()
                raise ValueError(
                    "URL returned an HTML webpage instead of a video stream (the Google Drive file may be private/restricted or require sign-in)."
                )

            # Determine filename and extension
            ext = '.mp4'
            if 'quicktime' in content_type or target_url.lower().endswith('.mov'):
                ext = '.mov'
            elif 'webm' in content_type or target_url.lower().endswith('.webm'):
                ext = '.webm'

            safe_base = re.sub(r'[^a-zA-Z0-9_-]', '_', (item.movement_name or 'workout_video').strip().lower())[:80] or 'workout_video'
            safe_name = f"{safe_base}{ext}"
            media_dir = cls.get_media_dir(item.organization_id)
            stored_filename = f"{item.id}_{safe_name}"
            full_path = media_dir / stored_filename

            sha256 = hashlib.sha256()
            total_bytes = 0
            with open(full_path, 'wb') as out_f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        out_f.write(chunk)
                        sha256.update(chunk)
                        total_bytes += len(chunk)
            resp.close()

            if total_bytes == 0:
                if full_path.exists():
                    full_path.unlink(missing_ok=True)
                raise ValueError("Downloaded video stream was empty (0 bytes).")

            resolved_mime = content_type.split(';')[0].strip() if content_type else 'video/mp4'
            if not resolved_mime.startswith('video/'):
                resolved_mime = 'video/mp4'

            rel_path = f"wod_videos/{item.organization_id}/{stored_filename}"
            file_rec = cls._register_file_record(
                item=item,
                rel_path=rel_path,
                file_name=safe_name,
                file_size=total_bytes,
                mime_type=resolved_mime,
                checksum=sha256.hexdigest(),
                actor=actor,
                db_alias=db_alias,
            )

            item.video_file = file_rec
            item.video_storage_path = rel_path
            item.video_file_name = safe_name
            item.video_file_size = total_bytes
            item.video_mime_type = resolved_mime
            item.video_download_status = 'COMPLETED'
            item.video_download_error = None
            item.video_downloaded_at = timezone.now()
            item.save(
                using=db_alias,
                update_fields=[
                    'video_file',
                    'video_storage_path',
                    'video_file_name',
                    'video_file_size',
                    'video_mime_type',
                    'video_download_status',
                    'video_download_error',
                    'video_downloaded_at',
                    'updated_at',
                ],
            )
            WODClassificationService.evaluate_item(item, db_alias=db_alias, save=True)
            return item

        except Exception as exc:
            error_msg = str(exc)[:500]
            logger.warning("WODVideoService.download_video_from_url failed for item=%s url=%s: %s", item.id, target_url, error_msg)
            item.video_download_status = 'FAILED'
            item.video_download_error = error_msg
            item.classification_status = 'NEEDS_REVIEW'
            item.is_wod_eligible = False
            item.save(
                using=db_alias,
                update_fields=[
                    'video_download_status',
                    'video_download_error',
                    'classification_status',
                    'is_wod_eligible',
                    'updated_at',
                ],
            )
            return item


# ===========================================================================
# 5. EXCEL / CSV IMPORT SERVICE (PREVIEW + CONFIRM)
# ===========================================================================

class WODImportService:
    """
    Parses workout content Excel (.xlsx) or CSV files, normalizes multi-value tags,
    detects setup/instruction vs movement rows, identifies missing classification fields,
    and executes confirmed imports with automatic video downloading.
    """

    # Header aliases -> canonical field key
    HEADER_MAP = {
        'name of the movement': 'movement_name',
        'movement name': 'movement_name',
        'movement': 'movement_name',
        'exercise': 'movement_name',
        'exercise name': 'movement_name',
        'name': 'movement_name',
        'description': 'description',
        'feedback': 'feedback',
        'ideal for | program': 'program',
        'ideal for - program': 'program',
        'program': 'program',
        'programs': 'program',
        'class type': 'program',
        'resistance | springs': 'resistance_springs',
        'resistance - springs': 'resistance_springs',
        'resistance': 'resistance_springs',
        'springs': 'resistance_springs',
        'breathing': 'breathing',
        'breathing notes': 'breathing',
        'regression': 'regression_text',
        'regression text': 'regression_text',
        'regression link': 'regression_url',
        'regression url': 'regression_url',
        'movement ideal for | section': 'section',
        'movement ideal for - section': 'section',
        'ideal for | section': 'section',
        'section': 'section',
        'sections': 'section',
        'intensity level': 'intensity',
        'intensity': 'intensity',
        'user level': 'user_level',
        'level': 'user_level',
        'muscle working': 'muscle',
        'muscle': 'muscle',
        'muscle group': 'muscle',
        'body target': 'body_target',
        'equipment': 'equipment',
        'shot by': 'shot_by',
        'shot date': 'shot_date',
        'editor': 'editor',
        'edit date': 'edit_date',
        'edit checked': 'edit_checked',
        'requirement cut': 'requirement_cut',
        'video link (edited)': 'video_url',
        'video link': 'video_url',
        'video url': 'video_url',
        'edited video link': 'video_url',
        'folder number': 'folder_number',
        'folder no': 'folder_number',
        'folder': 'folder_number',
        'cues 1': 'cue_1',
        'cue 1': 'cue_1',
        'cues 2': 'cue_2',
        'cue 2': 'cue_2',
        'cues 3': 'cue_3',
        'cue 3': 'cue_3',
        'for youtube | public': 'youtube_public',
        'for youtube': 'youtube_public',
        'youtube public': 'youtube_public',
        'yt link (private)': 'private_video_url',
        'yt link': 'private_video_url',
        'private video url': 'private_video_url',
        'link to refer for shoot': 'reference_shoot_url',
        'reference shoot url': 'reference_shoot_url',
        'content kind': 'content_kind',
    }

    @classmethod
    def _parse_xlsx_bytes(cls, file_bytes: bytes) -> List[Dict[str, str]]:
        """
        Parses an .xlsx file into a list of row dicts using Python's built-in zipfile + ElementTree
        (with zero external library dependency required).
        """
        rows_matrix: List[List[str]] = []
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            shared_strings: List[str] = []
            if 'xl/sharedStrings.xml' in zf.namelist():
                ss_Root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
                for si in ss_Root.iter():
                    if si.tag.endswith('}si') or si.tag == 'si':
                        texts = [
                            t.text or ''
                            for t in si.iter()
                            if (t.tag.endswith('}t') or t.tag == 't')
                        ]
                        shared_strings.append(''.join(texts))

            # Locate the first worksheet
            sheet_names = sorted(
                [n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
            )
            if not sheet_names:
                return []

            sheet_root = ET.fromstring(zf.read(sheet_names[0]))

            def col_letters_to_index(cell_ref: str) -> int:
                letters = ''.join(ch for ch in cell_ref if ch.isalpha()).upper()
                idx = 0
                for ch in letters:
                    idx = idx * 26 + (ord(ch) - ord('A') + 1)
                return max(0, idx - 1)

            for row_el in sheet_root.iter():
                if not (row_el.tag.endswith('}row') or row_el.tag == 'row'):
                    continue
                row_cells: Dict[int, str] = {}
                max_col = -1
                for cell_el in row_el:
                    if not (cell_el.tag.endswith('}c') or cell_el.tag == 'c'):
                        continue
                    cell_ref = cell_el.attrib.get('r', '')
                    col_idx = col_letters_to_index(cell_ref) if cell_ref else (max_col + 1)
                    max_col = max(max_col, col_idx)
                    cell_type = cell_el.attrib.get('t', '')
                    val_text = ''
                    if cell_type == 'inlineStr':
                        texts = [
                            t.text or ''
                            for t in cell_el.iter()
                            if (t.tag.endswith('}t') or t.tag == 't')
                        ]
                        val_text = ''.join(texts)
                    else:
                        v_el = next(
                            (c for c in cell_el if c.tag.endswith('}v') or c.tag == 'v'),
                            None,
                        )
                        if v_el is not None and v_el.text is not None:
                            raw_v = v_el.text
                            if cell_type == 's':
                                try:
                                    val_text = shared_strings[int(raw_v)]
                                except (ValueError, IndexError):
                                    val_text = raw_v
                            else:
                                val_text = raw_v
                    row_cells[col_idx] = val_text.strip()

                if max_col >= 0:
                    row_list = [row_cells.get(i, '') for i in range(max_col + 1)]
                    if any(cell for cell in row_list):
                        rows_matrix.append(row_list)

        if not rows_matrix:
            return []

        headers = [h.strip() for h in rows_matrix[0]]
        result: List[Dict[str, str]] = []
        for r in rows_matrix[1:]:
            row_dict = {}
            for idx, header in enumerate(headers):
                if header:
                    row_dict[header] = r[idx].strip() if idx < len(r) else ''
            if any(v for v in row_dict.values()):
                result.append(row_dict)
        return result

    @classmethod
    def parse_uploaded_file(cls, uploaded_file) -> List[Dict[str, str]]:
        """
        Accepts a Django UploadedFile (.xlsx or .csv) or list of dicts and returns raw row dicts.
        """
        if isinstance(uploaded_file, list):
            return uploaded_file

        filename = (getattr(uploaded_file, 'name', '') or '').lower()
        raw_bytes = uploaded_file.read()
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(0)

        if filename.endswith('.xlsx') or (raw_bytes[:4] == b'PK\x03\x04'):
            return cls._parse_xlsx_bytes(raw_bytes)

        # Fallback to CSV / TSV
        text = raw_bytes.decode('utf-8-sig', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
        return [
            {str(k).strip(): str(v or '').strip() for k, v in row.items() if k is not None}
            for row in reader
            if any(str(v or '').strip() for v in row.values())
        ]

    @classmethod
    def _split_multi_values(cls, raw_val: str) -> List[str]:
        if not raw_val or not raw_val.strip():
            return []
        # Split on comma, slash, semicolon, pipe, or newline
        parts = re.split(r'[,/;\|\n]+', raw_val)
        cleaned = []
        for p in parts:
            val = p.strip().strip('-•*').strip()
            if val and val.lower() not in ('n/a', 'na', 'none', '-', 'nil', 'null'):
                cleaned.append(val)
        return cleaned

    @classmethod
    def _parse_bool(cls, val: str) -> bool:
        return (val or '').strip().lower() in ('true', 'yes', 'y', '1', 'checked', 'done', 'public')

    @classmethod
    def _parse_date(cls, val: str) -> Optional[str]:
        raw = (val or '').strip()
        if not raw or raw.lower() in ('n/a', 'na', 'none', '-'):
            return None
        # Excel serial date number check
        if re.match(r'^\d{5}(\.\d+)?$', raw):
            try:
                serial = int(float(raw))
                from datetime import timedelta
                dt = date(1899, 12, 30) + timedelta(days=serial)
                return dt.isoformat()
            except Exception:
                pass
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(raw[:10] if fmt == '%Y-%m-%d' else raw, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    @classmethod
    def normalize_row(cls, raw_row: Dict[str, Any], row_number: int) -> Dict[str, Any]:
        mapped: Dict[str, str] = {}
        for k, v in raw_row.items():
            norm_k = re.sub(r'\s+', ' ', str(k or '').strip().lower())
            canonical = cls.HEADER_MAP.get(norm_k, norm_k)
            mapped[canonical] = str(v if v is not None else '').strip()

        movement_name = mapped.get('movement_name', '').strip()
        content_kind = WODClassificationService.detect_content_kind(
            movement_name, mapped.get('content_kind')
        )

        # Normalize tags by group
        tags_by_group: Dict[str, List[str]] = {
            'PROGRAM': [],
            'SECTION': [],
            'USER_LEVEL': [],
            'INTENSITY': [],
            'MUSCLE_GROUP': [],
            'BODY_TARGET': [],
            'BREATHING': [],
            'EQUIPMENT': [],
        }

        for p in cls._split_multi_values(mapped.get('program', '')):
            title_p = p.title() if p.islower() or p.isupper() else p
            if title_p not in tags_by_group['PROGRAM']:
                tags_by_group['PROGRAM'].append(title_p)

        for s in cls._split_multi_values(mapped.get('section', '')):
            norm_s = SECTION_SYNONYMS.get(s.lower(), s.title())
            if norm_s not in tags_by_group['SECTION']:
                tags_by_group['SECTION'].append(norm_s)

        for lvl in cls._split_multi_values(mapped.get('user_level', '')):
            norm_lvl = USER_LEVEL_SYNONYMS.get(lvl.lower(), lvl.title())
            if norm_lvl not in tags_by_group['USER_LEVEL']:
                tags_by_group['USER_LEVEL'].append(norm_lvl)

        for inten in cls._split_multi_values(mapped.get('intensity', '')):
            norm_i = INTENSITY_SYNONYMS.get(inten.lower(), inten.title())
            if norm_i not in tags_by_group['INTENSITY']:
                tags_by_group['INTENSITY'].append(norm_i)

        for m in cls._split_multi_values(mapped.get('muscle', '')):
            lower_m = m.lower()
            if lower_m in BODY_TARGET_CANONICAL:
                bt = BODY_TARGET_CANONICAL[lower_m]
                if bt not in tags_by_group['BODY_TARGET']:
                    tags_by_group['BODY_TARGET'].append(bt)
            else:
                mg = m.title()
                if mg not in tags_by_group['MUSCLE_GROUP']:
                    tags_by_group['MUSCLE_GROUP'].append(mg)

        for bt in cls._split_multi_values(mapped.get('body_target', '')):
            norm_bt = BODY_TARGET_CANONICAL.get(bt.lower(), bt.title())
            if norm_bt not in tags_by_group['BODY_TARGET']:
                tags_by_group['BODY_TARGET'].append(norm_bt)

        breathing_raw = mapped.get('breathing', '').strip()
        if breathing_raw and breathing_raw.lower() not in ('n/a', 'na', 'none', '-'):
            for b_item in re.split(r'[;\n\|]+', breathing_raw):
                b_clean = b_item.strip()
                if b_clean and b_clean not in tags_by_group['BREATHING']:
                    tags_by_group['BREATHING'].append(b_clean)

        for eq in cls._split_multi_values(mapped.get('equipment', '')):
            norm_eq = eq.title() if eq.islower() else eq
            if norm_eq not in tags_by_group['EQUIPMENT']:
                tags_by_group['EQUIPMENT'].append(norm_eq)

        # Also infer equipment from movement name or resistance_springs if helpful
        springs_raw = mapped.get('resistance_springs', '').strip()
        if springs_raw and springs_raw.lower() not in ('n/a', 'na', 'none', '-'):
            if any(k in springs_raw.lower() for k in ('red', 'blue', 'yellow', 'green', 'spring', 'reformer')):
                if 'Reformer' not in tags_by_group['EQUIPMENT']:
                    tags_by_group['EQUIPMENT'].append('Reformer')

        for eq_kw, eq_label in [
            ('loop band', 'Loop Band'),
            ('ankle weight', 'Ankle Weight'),
            ('trx', 'TRX'),
            ('footbar', 'Footbar'),
            ('dumbbell', 'Dumbbell'),
            ('reformer', 'Reformer'),
        ]:
            if eq_kw in movement_name.lower() and eq_label not in tags_by_group['EQUIPMENT']:
                tags_by_group['EQUIPMENT'].append(eq_label)

        video_url = mapped.get('video_url', '').strip()
        video_provider = WODVideoService.detect_provider_from_url(video_url)

        # Validate link & required classification fields
        issues: List[str] = []
        broken_video_link = False
        missing_video = not bool(video_url)

        if not movement_name:
            issues.append('Missing movement_name')
        if missing_video:
            issues.append('Missing video_url')
        elif not (video_url.startswith('http://') or video_url.startswith('https://')):
            broken_video_link = True
            issues.append('Invalid video URL format')
        elif 'drive.google.com/drive/folders/' in video_url.lower():
            broken_video_link = True
            issues.append('Video link points to a Google Drive folder instead of a file')

        missing_tags = False
        if content_kind == 'MOVEMENT':
            if not tags_by_group['PROGRAM']:
                missing_tags = True
                issues.append('Missing PROGRAM tag')
            if not tags_by_group['SECTION']:
                missing_tags = True
                issues.append('Missing SECTION tag')
            if not tags_by_group['INTENSITY']:
                missing_tags = True
                issues.append('Missing INTENSITY tag')
            if not (tags_by_group['MUSCLE_GROUP'] or tags_by_group['BODY_TARGET']):
                missing_tags = True
                issues.append('Missing MUSCLE_GROUP / BODY_TARGET tag')

        if broken_video_link:
            classification_status = 'NEEDS_REVIEW'
        elif issues:
            classification_status = 'INCOMPLETE'
        else:
            classification_status = 'COMPLETE'

        return {
            'row_number': row_number,
            'movement_name': movement_name,
            'content_kind': content_kind,
            'description': mapped.get('description') or None,
            'feedback': mapped.get('feedback') or None,
            'resistance_springs': springs_raw or None,
            'breathing_notes': breathing_raw or None,
            'regression_text': mapped.get('regression_text') or None,
            'regression_url': mapped.get('regression_url') or None,
            'video_provider': video_provider,
            'video_url': video_url or None,
            'private_video_url': mapped.get('private_video_url') or None,
            'folder_number': mapped.get('folder_number') or None,
            'cue_1': mapped.get('cue_1') or None,
            'cue_2': mapped.get('cue_2') or None,
            'cue_3': mapped.get('cue_3') or None,
            'shot_by': mapped.get('shot_by') or None,
            'shot_date': cls._parse_date(mapped.get('shot_date', '')),
            'editor': mapped.get('editor') or None,
            'edit_date': cls._parse_date(mapped.get('edit_date', '')),
            'edit_checked': cls._parse_bool(mapped.get('edit_checked', '')),
            'requirement_cut': mapped.get('requirement_cut') or None,
            'youtube_public': cls._parse_bool(mapped.get('youtube_public', '')),
            'reference_shoot_url': mapped.get('reference_shoot_url') or None,
            'tags_by_group': {k: v for k, v in tags_by_group.items() if v},
            'classification_status': classification_status,
            'is_wod_eligible': False,  # Never auto-eligible on import unless explicitly activated & complete
            'missing_video': missing_video,
            'broken_video_link': broken_video_link,
            'missing_tags': missing_tags,
            'issues': issues,
            'raw_import_data': raw_row,
        }

    @classmethod
    def preview_import(
        cls,
        organization: Organization,
        raw_rows: List[Dict[str, Any]],
        db_alias: str = 'default',
    ) -> Dict[str, Any]:
        existing_names = set(
            name.strip().lower()
            for name in WorkoutContentItem.objects.using(db_alias)
            .filter(organization=organization)
            .values_list('movement_name', flat=True)
            if name
        )

        seen_in_file = set()
        normalized_rows = []
        movement_count = 0
        setup_instruction_count = 0
        missing_video_count = 0
        broken_video_count = 0
        missing_tags_count = 0
        duplicate_count = 0
        complete_count = 0
        incomplete_count = 0
        needs_review_count = 0

        for idx, raw_row in enumerate(raw_rows, start=1):
            norm = cls.normalize_row(raw_row, row_number=idx)
            lower_name = norm['movement_name'].lower()
            is_duplicate = False
            if lower_name:
                if lower_name in seen_in_file or lower_name in existing_names:
                    is_duplicate = True
                seen_in_file.add(lower_name)

            norm['is_duplicate'] = is_duplicate
            norm['exists_in_db'] = lower_name in existing_names if lower_name else False
            if is_duplicate:
                duplicate_count += 1
                if 'Duplicate movement_name' not in norm['issues']:
                    norm['issues'].append('Duplicate movement_name')

            if norm['content_kind'] == 'MOVEMENT':
                movement_count += 1
            else:
                setup_instruction_count += 1

            if norm['missing_video']:
                missing_video_count += 1
            if norm['broken_video_link']:
                broken_video_count += 1
            if norm['missing_tags']:
                missing_tags_count += 1

            if norm['classification_status'] == 'COMPLETE':
                complete_count += 1
            elif norm['classification_status'] == 'NEEDS_REVIEW':
                needs_review_count += 1
            else:
                incomplete_count += 1

            normalized_rows.append(norm)

        return {
            'summary': {
                'total_rows': len(normalized_rows),
                'movement_rows': movement_count,
                'setup_instruction_rows': setup_instruction_count,
                'complete_rows': complete_count,
                'incomplete_rows': incomplete_count,
                'needs_review_rows': needs_review_count,
                'missing_video_rows': missing_video_count,
                'broken_video_rows': broken_video_count,
                'missing_tags_rows': missing_tags_count,
                'duplicate_rows': duplicate_count,
            },
            'rows': normalized_rows,
        }

    @classmethod
    @transaction.atomic
    def confirm_import(
        cls,
        organization: Organization,
        rows: List[Dict[str, Any]],
        actor: Optional[TenantUser] = None,
        duplicate_strategy: str = 'UPDATE',  # 'UPDATE' or 'SKIP'
        auto_download_videos: bool = True,
        db_alias: str = 'default',
    ) -> Dict[str, Any]:
        WODTagService.ensure_initial_tags(organization, db_alias=db_alias)

        created_count = 0
        updated_count = 0
        skipped_count = 0
        downloaded_videos_count = 0
        failed_video_downloads_count = 0
        imported_items: List[WorkoutContentItem] = []

        for idx, row_input in enumerate(rows, start=1):
            # Support either already-normalized rows (from preview) or raw rows
            if 'tags_by_group' in row_input and 'content_kind' in row_input:
                norm = row_input
            else:
                norm = cls.normalize_row(row_input, row_number=idx)

            movement_name = (norm.get('movement_name') or '').strip()
            if not movement_name:
                skipped_count += 1
                continue

            existing = (
                WorkoutContentItem.objects.using(db_alias)
                .filter(organization=organization, movement_name__iexact=movement_name)
                .first()
            )
            if existing and duplicate_strategy.upper() == 'SKIP':
                skipped_count += 1
                continue

            content_kind = WODClassificationService.detect_content_kind(
                movement_name, norm.get('content_kind')
            )
            video_url = (norm.get('video_url') or '').strip() or None
            video_provider = norm.get('video_provider') or WODVideoService.detect_provider_from_url(video_url)

            item_fields = {
                'movement_name': movement_name,
                'content_kind': content_kind,
                'description': norm.get('description') or None,
                'feedback': norm.get('feedback') or None,
                'resistance_springs': norm.get('resistance_springs') or None,
                'breathing_notes': norm.get('breathing_notes') or None,
                'regression_text': norm.get('regression_text') or None,
                'regression_url': norm.get('regression_url') or None,
                'video_provider': video_provider,
                'video_url': video_url,
                'private_video_url': norm.get('private_video_url') or None,
                'folder_number': norm.get('folder_number') or None,
                'cue_1': norm.get('cue_1') or None,
                'cue_2': norm.get('cue_2') or None,
                'cue_3': norm.get('cue_3') or None,
                'shot_by': norm.get('shot_by') or None,
                'shot_date': norm.get('shot_date') or None,
                'editor': norm.get('editor') or None,
                'edit_date': norm.get('edit_date') or None,
                'edit_checked': bool(norm.get('edit_checked', False)),
                'requirement_cut': norm.get('requirement_cut') or None,
                'youtube_public': bool(norm.get('youtube_public', False)),
                'reference_shoot_url': norm.get('reference_shoot_url') or None,
                'raw_import_data': norm.get('raw_import_data') or {},
            }

            if existing:
                for k, v in item_fields.items():
                    setattr(existing, k, v)
                if content_kind != 'MOVEMENT':
                    existing.is_wod_eligible = False
                existing.save(using=db_alias)
                item = existing
                updated_count += 1
            else:
                item = WorkoutContentItem(
                    organization=organization,
                    status=norm.get('status') or 'ACTIVE',
                    is_wod_eligible=False,
                    video_download_status='PENDING' if video_url else 'NONE',
                    **item_fields,
                )
                item.save(using=db_alias)
                created_count += 1

            # Sync normalized tags
            tags_by_group: Dict[str, List[str]] = norm.get('tags_by_group') or {}
            for group, tag_names in tags_by_group.items():
                for t_name in tag_names:
                    tag_obj = WODTagService.get_or_create_tag(
                        organization=organization,
                        tag_group=group,
                        name=t_name,
                        db_alias=db_alias,
                    )
                    if tag_obj:
                        WorkoutContentTag.objects.using(db_alias).get_or_create(
                            content_item=item,
                            tag=tag_obj,
                            defaults={'organization': organization},
                        )

            # Handle video download from link if video_url is present
            if video_url:
                if norm.get('broken_video_link'):
                    item.video_download_status = 'FAILED'
                    item.video_download_error = '; '.join(norm.get('issues') or ['Invalid video URL'])
                    item.classification_status = 'NEEDS_REVIEW'
                    item.is_wod_eligible = False
                    item.save(
                        using=db_alias,
                        update_fields=['video_download_status', 'video_download_error', 'classification_status', 'is_wod_eligible', 'updated_at'],
                    )
                    failed_video_downloads_count += 1
                elif auto_download_videos:
                    WODVideoService.download_video_from_url(
                        item=item,
                        url=video_url,
                        actor=actor,
                        db_alias=db_alias,
                        timeout=12,
                    )
                    if item.video_download_status == 'COMPLETED':
                        downloaded_videos_count += 1
                    elif item.video_download_status == 'FAILED':
                        failed_video_downloads_count += 1
                else:
                    item.video_download_status = 'PENDING'
                    item.save(using=db_alias, update_fields=['video_download_status', 'updated_at'])

            WODClassificationService.evaluate_item(item, db_alias=db_alias, save=True)
            imported_items.append(item)

        record_business_audit(
            organization=organization,
            module='ops',
            action_code='WOD_CONTENT_IMPORTED',
            entity_type='WorkoutContentItem',
            entity_id=organization.id,
            actor_user=actor if (actor and hasattr(actor, 'pk') and getattr(actor, '_auth_type', None) == 'tenant') else None,
            event_description=f"Imported WOD content items: {created_count} created, {updated_count} updated, {skipped_count} skipped.",
            after_data={
                'created_count': created_count,
                'updated_count': updated_count,
                'skipped_count': skipped_count,
                'downloaded_videos_count': downloaded_videos_count,
                'failed_video_downloads_count': failed_video_downloads_count,
            },
            db_alias=db_alias,
        )

        return {
            'created_count': created_count,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'downloaded_videos_count': downloaded_videos_count,
            'failed_video_downloads_count': failed_video_downloads_count,
            'total_processed': created_count + updated_count,
            'item_ids': [str(i.id) for i in imported_items],
        }
