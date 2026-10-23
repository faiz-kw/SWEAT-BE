"""
apps/tenant_core/models_community.py — Multi-Tenant Community & Social Transformations Engine

Provides full persistence and data models for the SWEAT Community:
1. CommunityCategory — Channel categories (Transformations, Highlights, Discussions, Announcements).
2. CommunityPost — Main transformation/story/highlight posts with Before & After photos,
   journey metrics (Duration, Weight/Muscle change), Coach attribution & rating, privacy scoping.
3. PostMedia — Additional photo/video gallery media attached to posts.
4. CommunityComment — Threaded 2-level nested comments with verified coach highlighting.
5. PostReaction — Social high-fives and reactions.
6. PostBookmark — Member bookmarking / saving inspiring posts.
7. CommunityReport — Member abuse & inappropriate content reporting.
8. MemberRequest — Voice Your Idea / gym suggestion box with upvoting and official admin responses.
9. MemberRequestVote — Upvotes for gym improvement requests.
10. UserSanction — Moderation penalties (Warnings, Mutes, Community Bans, Shadowbans).
11. CommunityAuditLog — Immutable log of all administrator and moderator actions.

All models belong strictly to tenant schema and include organization foreign keys for strict multi-tenancy.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile


class CommunityCategory(models.Model):
    """
    Channel / Topic categories for posts (e.g. Transformations, Highlights, Discussions, Announcements).
    Tenant-configurable with color badges and admin-only posting controls.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='community_categories',
        db_column='organization_id'
    )
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=60)
    description = models.TextField(blank=True, default='')
    icon_name = models.CharField(max_length=50, default='flame')
    color_hex = models.CharField(max_length=7, default='#E11D48')
    is_admin_only = models.BooleanField(
        default=False,
        help_text='If True, only verified coaches or admins can post in this channel (e.g. Announcements).'
    )
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_categories'
        unique_together = ('organization', 'slug')
        ordering = ['display_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.slug})"


class PostPrivacyScope(models.TextChoices):
    STUDIO_PUBLIC = 'STUDIO_PUBLIC', 'Studio Public'
    MEMBERS_ONLY = 'MEMBERS_ONLY', 'Members Only'
    ANONYMOUS = 'ANONYMOUS', 'Anonymous'


class PostStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending Approval'
    PUBLISHED = 'PUBLISHED', 'Published'
    FLAGGED = 'FLAGGED', 'Flagged for Review'
    HIDDEN = 'HIDDEN', 'Hidden by Moderator'
    REMOVED = 'REMOVED', 'Removed for Violation'


class CommunityPost(models.Model):
    """
    Central Community Post entity. Supports:
    - Member transformation journeys with Before & After photo comparison sliders
    - Workout journey metrics (Duration: e.g. '12 Weeks', Metric Change: e.g. '-11.5 kg')
    - Coach attribution and star ratings (e.g. 'Coached by Shweta Arora Chawla' - 5.0 ★)
    - Privacy scoping (Studio Public, Members Only, Anonymous)
    - High-performance atomic counters for likes, comments, and reports
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='community_posts',
        db_column='organization_id'
    )
    author_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='community_posts',
        db_column='author_profile_id'
    )
    category = models.ForeignKey(
        CommunityCategory,
        on_delete=models.PROTECT,
        related_name='posts',
        db_column='category_id'
    )

    # Journey Title & Content
    journey_title = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='Headline for the journey (e.g. "PCOD & Core Strength Journey")'
    )
    content = models.TextField(
        help_text='Full story and personal experience narrative'
    )

    # Transformation Before & After Photos (Interactive Slider in mobile & web)
    before_photo_url = models.URLField(max_length=600, blank=True, null=True)
    after_photo_url = models.URLField(max_length=600, blank=True, null=True)

    # Journey Metrics Chip (e.g. "🔥 -11.5 kg • 12 Weeks")
    duration_text = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text='e.g. "12 Weeks", "6 Months"'
    )
    metric_change = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text='e.g. "-11.5 kg", "+4.2 kg Muscle"'
    )

    # Coach / Trainer Attribution & Member Rating
    coach_profile = models.ForeignKey(
        TrainerProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attributed_community_posts',
        db_column='coach_profile_id'
    )
    coach_name_snapshot = models.CharField(
        max_length=150,
        blank=True,
        default='',
        help_text='Coach name e.g. "Shweta Arora Chawla"'
    )
    coach_rating = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
        help_text='Member satisfaction rating e.g. 5.0'
    )

    # Privacy & Access Control
    privacy_scope = models.CharField(
        max_length=25,
        choices=PostPrivacyScope.choices,
        default=PostPrivacyScope.STUDIO_PUBLIC
    )

    # Lifecycle & Moderation Status
    status = models.CharField(
        max_length=25,
        choices=PostStatus.choices,
        default=PostStatus.PUBLISHED,
        db_index=True
    )
    is_pinned = models.BooleanField(default=False, db_index=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    is_comments_disabled = models.BooleanField(default=False)
    is_deleted_by_author = models.BooleanField(default=False)

    # Denormalized performance counters (updated atomically via F-expressions)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    bookmarks_count = models.PositiveIntegerField(default=0)
    reports_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_posts'
        ordering = ['-is_pinned', '-created_at']
        indexes = [
            models.Index(fields=['organization', 'status', 'created_at'], name='idx_comm_posts_feed'),
            models.Index(fields=['organization', 'category', 'status'], name='idx_comm_posts_cat'),
            models.Index(fields=['author_profile', 'created_at'], name='idx_comm_posts_author'),
        ]

    def __str__(self):
        title = self.journey_title or self.content[:30]
        return f"[{self.organization.name}] {title} by {self.author_profile.first_name_snapshot or 'Member'}"


class PostMedia(models.Model):
    """
    Additional media items (images or video clips) attached to a community post.
    """
    class MediaType(models.TextChoices):
        IMAGE = 'IMAGE', 'Image'
        VIDEO = 'VIDEO', 'Video'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        CommunityPost,
        on_delete=models.CASCADE,
        related_name='media_items',
        db_column='post_id'
    )
    media_type = models.CharField(max_length=10, choices=MediaType.choices, default=MediaType.IMAGE)
    media_url = models.URLField(max_length=600)
    thumbnail_url = models.URLField(max_length=600, blank=True, null=True)
    width = models.PositiveIntegerField(default=1080)
    height = models.PositiveIntegerField(default=1080)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_post_media'
        ordering = ['display_order', 'created_at']

    def __str__(self):
        return f"{self.media_type} for Post {self.post_id}"


class CommunityComment(models.Model):
    """
    Threaded comments on community posts.
    Includes verified [COACH] badge identification for coaches encouraging members.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='community_comments',
        db_column='organization_id'
    )
    post = models.ForeignKey(
        CommunityPost,
        on_delete=models.CASCADE,
        related_name='comments',
        db_column='post_id'
    )
    author_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='community_comments',
        db_column='author_profile_id'
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replies',
        db_column='parent_id'
    )
    content = models.TextField(max_length=1500)
    is_coach_verified = models.BooleanField(
        default=False,
        help_text='Automatically flagged True if the author is a certified coach/trainer in this tenant'
    )
    is_hidden = models.BooleanField(default=False)
    likes_count = models.PositiveIntegerField(default=0)
    reports_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_comments'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['post', 'created_at'], name='idx_comm_cmts_post'),
            models.Index(fields=['parent', 'created_at'], name='idx_comm_cmts_parent'),
        ]

    def __str__(self):
        author = self.author_profile.first_name_snapshot or 'User'
        return f"Comment by {author} on {self.post_id}: {self.content[:30]}"


class PostReaction(models.Model):
    """
    Member reactions to posts (High-Five, Sweat Fire, Heart).
    """
    class ReactionType(models.TextChoices):
        HIGH_FIVE = 'HIGH_FIVE', 'High Five'
        SWEAT = 'SWEAT', 'Sweat Fire'
        HEART = 'HEART', 'Heart'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        CommunityPost,
        on_delete=models.CASCADE,
        related_name='reactions',
        db_column='post_id'
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='community_reactions',
        db_column='user_profile_id'
    )
    reaction_type = models.CharField(
        max_length=20,
        choices=ReactionType.choices,
        default=ReactionType.HIGH_FIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_post_reactions'
        unique_together = ('post', 'user_profile')

    def __str__(self):
        return f"{self.reaction_type} by {self.user_profile_id} on {self.post_id}"


class PostBookmark(models.Model):
    """
    Member saved / bookmarked community posts for later inspiration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        CommunityPost,
        on_delete=models.CASCADE,
        related_name='bookmarks',
        db_column='post_id'
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='community_bookmarks',
        db_column='user_profile_id'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_post_bookmarks'
        unique_together = ('post', 'user_profile')

    def __str__(self):
        return f"Bookmark by {self.user_profile_id} for {self.post_id}"


class CommunityReport(models.Model):
    """
    Member-submitted abuse, spam, or inappropriate content reports.
    """
    class ReportReason(models.TextChoices):
        SPAM = 'SPAM', 'Spam or Commercial Promotion'
        HARASSMENT = 'HARASSMENT', 'Harassment or Bullying'
        MISINFORMATION = 'MISINFORMATION', 'Misleading Information or Unsafe Advice'
        INAPPROPRIATE = 'INAPPROPRIATE', 'Inappropriate Imagery or Nudity'
        OFF_TOPIC = 'OFF_TOPIC', 'Irrelevant or Off-Topic Content'
        OTHER = 'OTHER', 'Other Violation'

    class ReportStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending Moderator Review'
        UPHELD = 'UPHELD', 'Upheld / Content Taken Down'
        DISMISSED = 'DISMISSED', 'Dismissed / Content Safe'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='community_reports',
        db_column='organization_id'
    )
    reporter_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='reports_submitted',
        db_column='reporter_profile_id'
    )
    post = models.ForeignKey(
        CommunityPost,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reports',
        db_column='post_id'
    )
    comment = models.ForeignKey(
        CommunityComment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reports',
        db_column='comment_id'
    )
    reason = models.CharField(max_length=30, choices=ReportReason.choices)
    custom_details = models.TextField(max_length=500, blank=True, default='')
    status = models.CharField(
        max_length=20,
        choices=ReportStatus.choices,
        default=ReportStatus.PENDING,
        db_index=True
    )
    reviewed_by = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='community_reports_reviewed',
        db_column='reviewed_by_id'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_reports'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status', 'created_at'], name='idx_comm_rpts_queue'),
        ]

    def __str__(self):
        return f"Report {self.reason} on Post {self.post_id or self.comment_id}"


class MemberRequest(models.Model):
    """
    "Voice Your Idea" — Member suggestions, equipment requests, and gym feedback.
    Features community upvoting and official gym management status & replies.
    """
    class RequestStatus(models.TextChoices):
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
        CONSIDERING = 'CONSIDERING', 'Under Consideration'
        PLANNED = 'PLANNED', 'Planned'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'
        DECLINED = 'DECLINED', 'Declined'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='member_requests',
        db_column='organization_id'
    )
    author_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='member_requests',
        db_column='author_profile_id'
    )
    title = models.CharField(max_length=150)
    description = models.TextField(max_length=2000)
    status = models.CharField(
        max_length=25,
        choices=RequestStatus.choices,
        default=RequestStatus.UNDER_REVIEW,
        db_index=True
    )
    upvotes_count = models.PositiveIntegerField(default=1)
    admin_response = models.TextField(blank=True, default='')
    responded_by = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member_requests_responded',
        db_column='responded_by_id'
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_member_requests'
        ordering = ['-upvotes_count', '-created_at']
        indexes = [
            models.Index(fields=['organization', 'status', 'upvotes_count'], name='idx_comm_reqs_status'),
        ]

    def __str__(self):
        return f"[{self.status}] {self.title} ({self.upvotes_count} upvotes)"


class MemberRequestVote(models.Model):
    """
    Unique upvote record for a member suggestion.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        MemberRequest,
        on_delete=models.CASCADE,
        related_name='votes',
        db_column='request_id'
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='request_votes',
        db_column='user_profile_id'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_member_request_votes'
        unique_together = ('request', 'user_profile')

    def __str__(self):
        return f"Vote by {self.user_profile_id} for Request {self.request_id}"


class UserSanction(models.Model):
    """
    Disciplinary actions issued against violating users.
    Supports official warnings, time-limited mutes, community bans, and shadowbans.
    """
    class SanctionType(models.TextChoices):
        WARNING = 'WARNING', 'Official Warning'
        MUTE = 'MUTE', 'Temporary Mute (No Posts/Comments)'
        BAN = 'BAN', 'Community Ban (Access Revoked)'
        SHADOWBAN = 'SHADOWBAN', 'Shadowban (Hidden from others)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='user_sanctions',
        db_column='organization_id'
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name='community_sanctions',
        db_column='user_profile_id'
    )
    sanction_type = models.CharField(max_length=20, choices=SanctionType.choices)
    reason = models.CharField(max_length=255)
    expires_at = models.DateTimeField(null=True, blank=True, help_text='Null represents permanent sanction')
    issued_by = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sanctions_issued',
        db_column='issued_by_id'
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_user_sanctions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'user_profile', 'is_active'], name='idx_comm_sanct_active'),
        ]

    def __str__(self):
        return f"{self.sanction_type} on {self.user_profile_id} (Active: {self.is_active})"


class CommunityAuditLog(models.Model):
    """
    Immutable audit trail tracking every single moderator and administrator action.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='community_audit_logs',
        db_column='organization_id'
    )
    actor = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='community_audit_actions',
        db_column='actor_id'
    )
    action = models.CharField(max_length=50)  # e.g. 'POST_HIDDEN', 'POST_REMOVED', 'USER_MUTED'
    target_type = models.CharField(max_length=30)  # 'POST', 'COMMENT', 'USER', 'REQUEST'
    target_id = models.CharField(max_length=64)
    reason = models.TextField(blank=True, default='')
    snapshot = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'community_audit_logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'created_at'], name='idx_comm_audit_time'),
        ]

    def __str__(self):
        actor_name = self.actor.email if self.actor else 'System'
        return f"{self.action} on {self.target_type}:{self.target_id} by {actor_name}"
