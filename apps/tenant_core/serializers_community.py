"""
apps/tenant_core/serializers_community.py — Serializers for Community & Transformations Module
"""

from rest_framework import serializers
from .models_community import (
    CommunityCategory, CommunityPost, PostMedia, CommunityComment,
    PostReaction, PostBookmark, CommunityReport, MemberRequest,
    MemberRequestVote, UserSanction, CommunityAuditLog,
    PostPrivacyScope, PostStatus
)
from .models_workforce import UserProfile, TrainerProfile


class CommunityCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = CommunityCategory
        fields = [
            'id', 'name', 'slug', 'description', 'icon_name',
            'color_hex', 'is_admin_only', 'display_order', 'is_active'
        ]


class PostMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMedia
        fields = [
            'id', 'media_type', 'media_url', 'thumbnail_url',
            'width', 'height', 'display_order'
        ]


class CommunityCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar = serializers.SerializerMethodField()
    replies_count = serializers.SerializerMethodField()

    class Meta:
        model = CommunityComment
        fields = [
            'id', 'post_id', 'parent_id', 'content', 'is_coach_verified',
            'is_hidden', 'likes_count', 'reports_count', 'author_name',
            'author_avatar', 'replies_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'is_coach_verified', 'is_hidden', 'likes_count', 'reports_count', 'created_at']

    def get_author_name(self, obj):
        profile = getattr(obj, 'author_profile', None)
        if not profile:
            return "Member"
        first = profile.first_name_snapshot or ""
        last = profile.last_name_snapshot or ""
        name = f"{first} {last}".strip()
        return name or "Member"

    def get_author_avatar(self, obj):
        profile = getattr(obj, 'author_profile', None)
        if profile and profile.profile_file:
            return profile.profile_file.url
        return None

    def get_replies_count(self, obj):
        if hasattr(obj, 'replies'):
            return obj.replies.count()
        return 0


class CommunityPostSerializer(serializers.ModelSerializer):
    category = CommunityCategorySerializer(read_only=True)
    category_slug = serializers.CharField(source='category.slug', read_only=True)
    author_name = serializers.SerializerMethodField()
    author_avatar = serializers.SerializerMethodField()
    author_role = serializers.SerializerMethodField()
    media_items = PostMediaSerializer(many=True, read_only=True)
    is_liked_by_me = serializers.SerializerMethodField()
    is_bookmarked_by_me = serializers.SerializerMethodField()
    highlighted_comment = serializers.SerializerMethodField()

    class Meta:
        model = CommunityPost
        fields = [
            'id', 'journey_title', 'content', 'category', 'category_slug',
            'before_photo_url', 'after_photo_url', 'duration_text', 'metric_change',
            'coach_name_snapshot', 'coach_rating', 'privacy_scope', 'status',
            'is_pinned', 'pinned_at', 'is_comments_disabled',
            'likes_count', 'comments_count', 'bookmarks_count', 'reports_count',
            'author_name', 'author_avatar', 'author_role', 'media_items',
            'is_liked_by_me', 'is_bookmarked_by_me', 'highlighted_comment',
            'created_at', 'updated_at'
        ]

    def get_author_name(self, obj):
        if obj.privacy_scope == PostPrivacyScope.ANONYMOUS:
            return "Anonymous Member"
        profile = getattr(obj, 'author_profile', None)
        if not profile:
            return "SWEAT Member"
        first = profile.first_name_snapshot or ""
        last = profile.last_name_snapshot or ""
        name = f"{first} {last}".strip()
        return name or "SWEAT Member"

    def get_author_avatar(self, obj):
        if obj.privacy_scope == PostPrivacyScope.ANONYMOUS:
            return None
        profile = getattr(obj, 'author_profile', None)
        if profile and profile.profile_file:
            return profile.profile_file.url
        return None

    def get_author_role(self, obj):
        profile = getattr(obj, 'author_profile', None)
        if not profile or not profile.user:
            return "MEMBER"
        user = profile.user
        if getattr(user, 'user_type', None) in ['ADMIN', 'ORG_ADMIN', 'STAFF']:
            if getattr(user, 'trainer_profile', None):
                return "COACH"
            return "STAFF"
        return "MEMBER"

    def get_is_liked_by_me(self, obj):
        current_user_profile = self.context.get('current_user_profile')
        if not current_user_profile:
            return False
        # Cached in context for bulk feed queries
        liked_ids = self.context.get('liked_post_ids')
        if liked_ids is not None:
            return obj.id in liked_ids
        return PostReaction.objects.filter(post=obj, user_profile=current_user_profile).exists()

    def get_is_bookmarked_by_me(self, obj):
        current_user_profile = self.context.get('current_user_profile')
        if not current_user_profile:
            return False
        bookmarked_ids = self.context.get('bookmarked_post_ids')
        if bookmarked_ids is not None:
            return obj.id in bookmarked_ids
        return PostBookmark.objects.filter(post=obj, user_profile=current_user_profile).exists()

    def get_highlighted_comment(self, obj):
        """Returns the top coach comment or latest comment to display on the feed card preview."""
        # Prioritize coach verified comments
        coach_comment = obj.comments.filter(is_hidden=False, is_coach_verified=True).order_by('-created_at').first()
        if coach_comment:
            return CommunityCommentSerializer(coach_comment, context=self.context).data
        latest = obj.comments.filter(is_hidden=False).order_by('-created_at').first()
        if latest:
            return CommunityCommentSerializer(latest, context=self.context).data
        return None


class CommunityPostCreateSerializer(serializers.ModelSerializer):
    category_slug = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = CommunityPost
        fields = [
            'category_slug', 'journey_title', 'content',
            'before_photo_url', 'after_photo_url',
            'duration_text', 'metric_change',
            'coach_name_snapshot', 'coach_rating',
            'privacy_scope'
        ]

    def validate_content(self, value):
        if not value or len(value.strip()) < 5:
            raise serializers.ValidationError("Post story content must be at least 5 characters.")
        return value.strip()


class CommunityReportCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommunityReport
        fields = ['post', 'comment', 'reason', 'custom_details']

    def validate(self, attrs):
        if not attrs.get('post') and not attrs.get('comment'):
            raise serializers.ValidationError("Either a post or a comment must be specified for reporting.")
        return attrs


class MemberRequestSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    is_voted_by_me = serializers.SerializerMethodField()
    responded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MemberRequest
        fields = [
            'id', 'title', 'description', 'status', 'upvotes_count',
            'admin_response', 'responded_at', 'responded_by_name',
            'author_name', 'is_voted_by_me', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'status', 'upvotes_count', 'admin_response', 'responded_at', 'created_at']

    def get_author_name(self, obj):
        profile = getattr(obj, 'author_profile', None)
        if not profile:
            return "Member"
        first = profile.first_name_snapshot or ""
        last = profile.last_name_snapshot or ""
        return f"{first} {last}".strip() or "Member"

    def get_is_voted_by_me(self, obj):
        current_user_profile = self.context.get('current_user_profile')
        if not current_user_profile:
            return False
        voted_ids = self.context.get('voted_request_ids')
        if voted_ids is not None:
            return obj.id in voted_ids
        return MemberRequestVote.objects.filter(request=obj, user_profile=current_user_profile).exists()

    def get_responded_by_name(self, obj):
        if obj.responded_by:
            first = obj.responded_by.first_name or ""
            last = obj.responded_by.last_name or ""
            return f"{first} {last}".strip() or "SWEAT Management"
        return None


class UserSanctionSerializer(serializers.ModelSerializer):
    issued_by_name = serializers.SerializerMethodField()
    member_name = serializers.SerializerMethodField()

    class Meta:
        model = UserSanction
        fields = [
            'id', 'user_profile_id', 'member_name', 'sanction_type',
            'reason', 'expires_at', 'is_active', 'issued_by_name', 'created_at'
        ]

    def get_issued_by_name(self, obj):
        if obj.issued_by:
            return f"{obj.issued_by.first_name} {obj.issued_by.last_name}".strip()
        return "System"

    def get_member_name(self, obj):
        profile = getattr(obj, 'user_profile', None)
        if profile:
            return f"{profile.first_name_snapshot} {profile.last_name_snapshot}".strip()
        return "Member"


class CommunityAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = CommunityAuditLog
        fields = [
            'id', 'actor_name', 'action', 'target_type',
            'target_id', 'reason', 'snapshot', 'ip_address', 'created_at'
        ]

    def get_actor_name(self, obj):
        if obj.actor:
            return f"{obj.actor.first_name} {obj.actor.last_name}".strip() or obj.actor.email
        return "System"
