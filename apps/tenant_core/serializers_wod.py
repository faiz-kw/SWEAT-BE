"""
Workout of the Day (WOD) Phase 1 — DRF Serializers
"""

from typing import Any, Dict, List
from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError

from .models_wod import WorkoutContentItem, WorkoutContentTag, WorkoutTag
from .services_wod import (
    WODClassificationService,
    WODTagService,
    WODVideoService,
    slugify_tag_code,
)


class WorkoutTagSerializer(serializers.ModelSerializer):
    code = serializers.CharField(max_length=100, required=False, allow_blank=True)
    usage_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkoutTag
        fields = [
            'id',
            'organization',
            'tag_group',
            'code',
            'name',
            'description',
            'display_order',
            'status',
            'usage_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'usage_count', 'created_at', 'updated_at']

    def get_usage_count(self, obj: WorkoutTag) -> int:
        if hasattr(obj, '_usage_count'):
            return obj._usage_count
        db_alias = self.context.get('db_alias') or getattr(obj._state, 'db', None) or 'default'
        return WorkoutContentTag.objects.using(db_alias).filter(tag=obj).count()

    def validate(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        name = (attrs.get('name') or getattr(self.instance, 'name', '') or '').strip()
        if not name:
            raise serializers.ValidationError({'name': 'Tag name is required.'})
        attrs['name'] = name

        raw_code = (attrs.get('code') or getattr(self.instance, 'code', '') or '').strip()
        code = slugify_tag_code(raw_code if raw_code else name)
        attrs['code'] = code

        tag_group = (attrs.get('tag_group') or getattr(self.instance, 'tag_group', '') or '').strip().upper()
        if not tag_group:
            raise serializers.ValidationError({'tag_group': 'tag_group is required.'})
        attrs['tag_group'] = tag_group

        org = self.context.get('organization') or getattr(self.instance, 'organization', None)
        db_alias = self.context.get('db_alias') or 'default'
        if org:
            qs = WorkoutTag.objects.using(db_alias).filter(
                organization=org,
                tag_group=tag_group,
                code=code,
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {'code': f"A tag with code '{code}' already exists in group '{tag_group}'."}
                )
        return attrs


class WorkoutTagCompactSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkoutTag
        fields = ['id', 'tag_group', 'code', 'name', 'status', 'display_order']


class WorkoutContentItemSerializer(serializers.ModelSerializer):
    tags = serializers.SerializerMethodField()
    tags_by_group = serializers.SerializerMethodField()
    has_video = serializers.BooleanField(read_only=True)
    has_stored_video = serializers.BooleanField(read_only=True)
    playback_video_url = serializers.SerializerMethodField()
    missing_requirements = serializers.SerializerMethodField()

    class Meta:
        model = WorkoutContentItem
        fields = [
            'id',
            'organization',
            'movement_name',
            'description',
            'feedback',
            'resistance_springs',
            'breathing_notes',
            'regression_text',
            'regression_url',
            'video_provider',
            'video_url',
            'private_video_url',
            'video_file',
            'video_storage_path',
            'video_file_name',
            'video_file_size',
            'video_mime_type',
            'video_download_status',
            'video_download_error',
            'video_downloaded_at',
            'has_video',
            'has_stored_video',
            'playback_video_url',
            'folder_number',
            'cue_1',
            'cue_2',
            'cue_3',
            'shot_by',
            'shot_date',
            'editor',
            'edit_date',
            'edit_checked',
            'requirement_cut',
            'youtube_public',
            'reference_shoot_url',
            'content_kind',
            'is_wod_eligible',
            'classification_status',
            'missing_requirements',
            'status',
            'tags',
            'tags_by_group',
            'raw_import_data',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'organization',
            'video_file',
            'video_storage_path',
            'video_file_name',
            'video_file_size',
            'video_mime_type',
            'video_downloaded_at',
            'has_video',
            'has_stored_video',
            'playback_video_url',
            'missing_requirements',
            'created_at',
            'updated_at',
        ]

    def _get_prefetched_tags(self, obj: WorkoutContentItem) -> List[WorkoutTag]:
        if hasattr(obj, '_prefetched_objects_cache') and 'content_tags' in obj._prefetched_objects_cache:
            return [
                ct.tag
                for ct in obj.content_tags.all()
                if ct.tag is not None
            ]
        db_alias = self.context.get('db_alias') or getattr(obj._state, 'db', None) or 'default'
        return [
            ct.tag
            for ct in WorkoutContentTag.objects.using(db_alias)
            .filter(content_item=obj)
            .select_related('tag')
            if ct.tag is not None
        ]

    def get_tags(self, obj: WorkoutContentItem) -> List[Dict[str, Any]]:
        tags = self._get_prefetched_tags(obj)
        return WorkoutTagCompactSerializer(tags, many=True).data

    def get_tags_by_group(self, obj: WorkoutContentItem) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for t in self._get_prefetched_tags(obj):
            grouped.setdefault(t.tag_group, []).append(WorkoutTagCompactSerializer(t).data)
        return grouped

    def get_playback_video_url(self, obj: WorkoutContentItem) -> str:
        if obj.has_stored_video:
            return f"/api/v1/tenant/wod/content-items/{obj.id}/stream-video/"
        return obj.video_url or ''

    def get_missing_requirements(self, obj: WorkoutContentItem) -> List[str]:
        tags = [t for t in self._get_prefetched_tags(obj) if t.status == 'ACTIVE']
        groups = {t.tag_group for t in tags}
        missing: List[str] = []
        if not (obj.movement_name and obj.movement_name.strip()):
            missing.append('Movement name is required')
        video_failed = obj.video_download_status == 'FAILED'
        has_valid_video = bool(
            obj.has_stored_video or (obj.video_url and obj.video_url.strip() and not video_failed)
        )
        if video_failed:
            missing.append(f"Video download failed ({obj.video_download_error or 'restricted or invalid link'})")
        elif not has_valid_video:
            missing.append('Video attachment or valid video link is required')

        if obj.content_kind == 'MOVEMENT':
            if 'PROGRAM' not in groups:
                missing.append('At least 1 PROGRAM tag is required')
            if 'SECTION' not in groups:
                missing.append('At least 1 SECTION tag is required')
            if 'INTENSITY' not in groups:
                missing.append('At least 1 INTENSITY tag is required')
            if not ({'MUSCLE_GROUP', 'BODY_TARGET'} & groups):
                missing.append('At least 1 MUSCLE_GROUP or BODY_TARGET tag is required')
        return missing
