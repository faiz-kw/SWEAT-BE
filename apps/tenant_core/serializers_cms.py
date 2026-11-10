from rest_framework import serializers
from apps.tenant_core.models_cms import CMSBanner, ProgramCategoryMedia, CMSMediaAsset
from apps.tenant_core.models_org import Branch
from apps.tenant_core.models_catalog import ProgramCategory
from apps.tenant_core.models_classes import ClassCategory


class CMSBannerSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    placement_display = serializers.CharField(source='get_placement_display', read_only=True)

    class Meta:
        model = CMSBanner
        fields = [
            'id', 'organization', 'branch', 'branch_name',
            'title', 'subtitle', 'placement', 'placement_display',
            'media_type', 'media_url', 'thumbnail_url', 'media_file',
            'link_type', 'link_target',
            'display_order', 'is_active', 'start_date', 'end_date',
            'created_at', 'updated_at', 'created_by',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at', 'created_by']


class ProgramCategoryMediaSerializer(serializers.ModelSerializer):
    program_category_name = serializers.CharField(source='program_category.name', read_only=True)
    class_category_name = serializers.CharField(source='class_category.name', read_only=True)

    class Meta:
        model = ProgramCategoryMedia
        fields = [
            'id', 'organization',
            'program_category', 'program_category_name',
            'class_category', 'class_category_name',
            'category_name', 'category_code', 'tagline',
            'icon_url', 'banner_url', 'content_description',
            'video_url', 'showcase_images',
            'display_order', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class CMSMediaAssetSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CMSMediaAsset
        fields = [
            'id', 'organization', 'title', 'description', 'media_type',
            'media_url', 'thumbnail_url', 'media_file',
            'duration_seconds', 'file_size_bytes', 'mime_type', 'tags',
            'is_published', 'created_at', 'updated_at', 'uploaded_by',
            'uploaded_by_name',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at', 'uploaded_by']

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            return f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip() or obj.uploaded_by.email
        return None
