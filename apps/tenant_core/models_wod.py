"""
Dedicated Tenant DB — Workout of the Day (WOD) Phase 1 Models
Tables:
  1. workout_content_items
  2. workout_tags
  3. workout_content_tags

Strictly scoped to Phase 1: Admin-Only Workout Content Library / Content Studio.
"""

import uuid
from django.db import models
from .models_org import Organization
from .models_infra import File


class WorkoutContentItem(models.Model):
    """
    Stores a single workout movement, setup, instruction, or technique video/content record.
    Supports both direct video uploads and downloaded video files from external links (e.g. Google Drive).
    """

    VIDEO_PROVIDER_CHOICES = [
        ('UPLOAD', 'Direct Upload'),
        ('GOOGLE_DRIVE', 'Google Drive'),
        ('YOUTUBE', 'YouTube'),
        ('ZATA', 'Zata Storage'),
        ('EXTERNAL', 'External URL'),
    ]

    VIDEO_DOWNLOAD_STATUS_CHOICES = [
        ('NONE', 'No Video'),
        ('PENDING', 'Pending Download'),
        ('DOWNLOADING', 'Downloading'),
        ('COMPLETED', 'Stored / Ready'),
        ('FAILED', 'Download Failed'),
    ]

    CONTENT_KIND_CHOICES = [
        ('MOVEMENT', 'Movement'),
        ('SETUP', 'Setup'),
        ('INSTRUCTION', 'Instruction'),
        ('TECHNIQUE', 'Technique'),
        ('OTHER', 'Other'),
    ]

    CLASSIFICATION_STATUS_CHOICES = [
        ('COMPLETE', 'Complete'),
        ('INCOMPLETE', 'Incomplete'),
        ('NEEDS_REVIEW', 'Needs Review'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='workout_content_items',
        db_column='organization_id',
    )

    movement_name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    feedback = models.TextField(null=True, blank=True)
    resistance_springs = models.TextField(null=True, blank=True)
    breathing_notes = models.TextField(null=True, blank=True)

    regression_text = models.TextField(null=True, blank=True)
    regression_url = models.TextField(null=True, blank=True)

    video_provider = models.CharField(
        max_length=30,
        choices=VIDEO_PROVIDER_CHOICES,
        null=True,
        blank=True,
    )
    video_url = models.TextField(null=True, blank=True)
    private_video_url = models.TextField(null=True, blank=True)

    # Managed video file attachment (populated via direct upload OR automatic download from video_url)
    video_file = models.ForeignKey(
        File,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='wod_content_items',
    )
    video_storage_path = models.TextField(null=True, blank=True)
    video_file_name = models.CharField(max_length=255, null=True, blank=True)
    video_file_size = models.BigIntegerField(null=True, blank=True)
    video_mime_type = models.CharField(max_length=100, null=True, blank=True)
    video_download_status = models.CharField(
        max_length=30,
        choices=VIDEO_DOWNLOAD_STATUS_CHOICES,
        default='NONE',
    )
    video_download_error = models.TextField(null=True, blank=True)
    video_downloaded_at = models.DateTimeField(null=True, blank=True)

    folder_number = models.CharField(max_length=100, null=True, blank=True)

    cue_1 = models.TextField(null=True, blank=True)
    cue_2 = models.TextField(null=True, blank=True)
    cue_3 = models.TextField(null=True, blank=True)

    shot_by = models.CharField(max_length=150, null=True, blank=True)
    shot_date = models.DateField(null=True, blank=True)

    editor = models.CharField(max_length=150, null=True, blank=True)
    edit_date = models.DateField(null=True, blank=True)
    edit_checked = models.BooleanField(default=False)

    requirement_cut = models.TextField(null=True, blank=True)
    youtube_public = models.BooleanField(default=False)
    reference_shoot_url = models.TextField(null=True, blank=True)

    content_kind = models.CharField(
        max_length=30,
        choices=CONTENT_KIND_CHOICES,
        default='MOVEMENT',
    )
    is_wod_eligible = models.BooleanField(default=False)
    classification_status = models.CharField(
        max_length=30,
        choices=CLASSIFICATION_STATUS_CHOICES,
        default='INCOMPLETE',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='DRAFT',
    )

    # Retains raw Excel row dictionary for reference/audit without driving filter logic
    raw_import_data = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'workout_content_items'
        ordering = ['movement_name', '-created_at']
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_wod_item_org_status'),
            models.Index(fields=['organization', 'content_kind'], name='idx_wod_item_org_kind'),
            models.Index(fields=['organization', 'is_wod_eligible'], name='idx_wod_item_org_elig'),
            models.Index(fields=['organization', 'classification_status'], name='idx_wod_item_org_class'),
        ]

    def __str__(self):
        return f"{self.movement_name} [{self.content_kind}] ({self.status})"

    @property
    def has_video(self) -> bool:
        return bool(
            (self.video_storage_path and self.video_download_status == 'COMPLETED')
            or self.video_file_id
            or (self.video_url and self.video_url.strip())
        )

    @property
    def has_stored_video(self) -> bool:
        return bool(
            (self.video_storage_path and self.video_download_status == 'COMPLETED')
            or self.video_file_id
        )


class WorkoutTag(models.Model):
    """
    Configurable normalized tag master for Workout Content Library.
    Supports dynamic admin-managed tag values across groups without schema migrations.
    """

    TAG_GROUP_CHOICES = [
        ('PROGRAM', 'Program / Class Type'),
        ('SECTION', 'Workout Section'),
        ('USER_LEVEL', 'User Level'),
        ('INTENSITY', 'Intensity Level'),
        ('MUSCLE_GROUP', 'Muscle Group'),
        ('BODY_TARGET', 'Body Target'),
        ('BREATHING', 'Breathing Pattern'),
        ('EQUIPMENT', 'Equipment'),
        ('MOVEMENT_FAMILY', 'Movement Family'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='workout_tags',
        db_column='organization_id',
    )
    tag_group = models.CharField(max_length=50, choices=TAG_GROUP_CHOICES)
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    description = models.TextField(null=True, blank=True)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'workout_tags'
        ordering = ['tag_group', 'display_order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'tag_group', 'code'],
                name='uq_workout_tag_org_group_code',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'tag_group', 'status'], name='idx_wod_tag_org_grp_st'),
        ]

    def __str__(self):
        return f"[{self.tag_group}] {self.name} ({self.code})"


class WorkoutContentTag(models.Model):
    """
    Many-to-many mapping between workout_content_items and workout_tags.
    Allows multiple tags from the same or different tag groups per movement.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='workout_content_tags',
        db_column='organization_id',
    )
    content_item = models.ForeignKey(
        WorkoutContentItem,
        on_delete=models.CASCADE,
        related_name='content_tags',
        db_column='content_item_id',
    )
    tag = models.ForeignKey(
        WorkoutTag,
        on_delete=models.CASCADE,
        related_name='content_tags',
        db_column='tag_id',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'workout_content_tags'
        ordering = ['tag__tag_group', 'tag__display_order', 'tag__name']
        constraints = [
            models.UniqueConstraint(
                fields=['content_item', 'tag'],
                name='uq_workout_content_item_tag',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'tag'], name='idx_wod_ctag_org_tag'),
            models.Index(fields=['content_item', 'tag'], name='idx_wod_ctag_item_tag'),
        ]

    def __str__(self):
        return f"{self.content_item_id} -> {self.tag_id}"
