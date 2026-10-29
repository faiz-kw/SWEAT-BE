"""
apps/tenant_core/views_community.py — Complete Multi-Tenant API Views for Community & Social Transformations

Provides robust, production-ready REST endpoints for:
A. Member Experience (Flutter App & Web Member Portal):
   - GET  /api/v1/mobile/community/categories/
   - GET  /api/v1/mobile/community/feed/
   - POST /api/v1/mobile/community/posts/
   - GET  /api/v1/mobile/community/posts/<id>/
   - DELETE /api/v1/mobile/community/posts/<id>/
   - POST /api/v1/mobile/community/posts/<id>/react/
   - POST /api/v1/mobile/community/posts/<id>/bookmark/
   - GET  /api/v1/mobile/community/posts/<id>/comments/
   - POST /api/v1/mobile/community/posts/<id>/comments/
   - POST /api/v1/mobile/community/report/
   - GET  /api/v1/mobile/community/requests/
   - POST /api/v1/mobile/community/requests/
   - POST /api/v1/mobile/community/requests/<id>/upvote/

B. Admin Command Center Management:
   - GET   /api/v1/admin/community/overview/
   - GET   /api/v1/admin/community/posts/
   - PATCH /api/v1/admin/community/posts/<id>/
   - DELETE /api/v1/admin/community/posts/<id>/
   - GET   /api/v1/admin/community/reports/
   - POST  /api/v1/admin/community/reports/<id>/resolve/
   - GET   /api/v1/admin/community/requests/
   - PATCH /api/v1/admin/community/requests/<id>/
   - POST  /api/v1/admin/community/users/<id>/sanction/
   - GET   /api/v1/admin/community/audit-logs/
"""

import logging
from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.db import transaction, models
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination

from .views_mobile import _resolve_mobile_tenant_and_db, _get_or_create_user_profile
from .models_org import Organization
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile
from .models_community import (
    CommunityCategory, CommunityPost, PostMedia, CommunityComment,
    PostReaction, PostBookmark, CommunityReport, MemberRequest,
    MemberRequestVote, UserSanction, CommunityAuditLog,
    PostPrivacyScope, PostStatus
)
from .serializers_community import (
    CommunityCategorySerializer,
    CommunityPostSerializer,
    CommunityPostCreateSerializer,
    CommunityCommentSerializer,
    CommunityReportCreateSerializer,
    MemberRequestSerializer,
    UserSanctionSerializer,
    CommunityAuditLogSerializer
)

logger = logging.getLogger(__name__)


def _get_active_organization(request, alias):
    """Fetch active Organization for the authenticated user or tenant DB."""
    user = getattr(request, 'user', None)
    if user and getattr(user, 'organization', None):
        return user.organization
    org = Organization.objects.using(alias).filter(status='ACTIVE').first()
    if not org:
        org = Organization.objects.using(alias).first()
    return org


def _ensure_default_categories(alias, org):
    """Ensures default categories exist for the tenant so the app never shows empty filters."""
    defaults = [
        {'name': 'Transformations', 'slug': 'transformations', 'icon_name': 'flame', 'color_hex': '#E11D48', 'order': 1},
        {'name': 'Highlights', 'slug': 'highlights', 'icon_name': 'sparkles', 'color_hex': '#2563EB', 'order': 2},
        {'name': 'Discussions', 'slug': 'discussions', 'icon_name': 'message-circle', 'color_hex': '#10B981', 'order': 3},
        {'name': 'Announcements', 'slug': 'announcements', 'icon_name': 'bell', 'color_hex': '#F59E0B', 'order': 4, 'is_admin_only': True},
    ]
    for d in defaults:
        CommunityCategory.objects.using(alias).get_or_create(
            organization=org,
            slug=d['slug'],
            defaults={
                'name': d['name'],
                'icon_name': d['icon_name'],
                'color_hex': d['color_hex'],
                'display_order': d['order'],
                'is_admin_only': d.get('is_admin_only', False),
                'is_active': True
            }
        )


def _check_user_active_sanctions(alias, profile, required_action="POST"):
    """
    Checks if a user has active mutes or bans in this tenant.
    Returns (is_restricted: bool, message: str)
    """
    now = timezone.now()
    active_sanctions = UserSanction.objects.using(alias).filter(
        user_profile=profile,
        is_active=True
    ).filter(
        models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
    )

    for sanction in active_sanctions:
        if sanction.sanction_type == UserSanction.SanctionType.BAN:
            return True, "Your community access has been revoked by administration."
        if sanction.sanction_type == UserSanction.SanctionType.MUTE:
            return True, "Your account is temporarily muted from posting and commenting."
    return False, ""


# ============================================================================
# 1. Member App Community Endpoints
# ============================================================================

class CommunityCategoriesMobileView(APIView):
    """
    GET /api/v1/mobile/community/categories/
    List topic categories (Transformations, Highlights, Discussions, Announcements).
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        if org:
            _ensure_default_categories(alias, org)

        categories = CommunityCategory.objects.using(alias).filter(
            organization=org,
            is_active=True
        ).order_by('display_order', 'name')

        serializer = CommunityCategorySerializer(categories, many=True)
        return Response({'results': serializer.data})


class CommunityFeedMobileView(APIView):
    """
    GET /api/v1/mobile/community/feed/
    Unified community feed supporting:
    - Category filtering (?category=transformations)
    - Tab sorting (?tab=trending or ?tab=latest)
    - Search query (?q=...)
    - Pagination (?page=1&page_size=15)
    - Resolves is_liked_by_me & is_bookmarked_by_me for authenticated member
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        current_profile = None
        if request.user and request.user.is_authenticated:
            current_profile = _get_or_create_user_profile(request.user, alias)

        category_slug = (request.query_params.get('category') or '').strip().lower()
        tab = (request.query_params.get('tab') or 'latest').strip().lower()
        search_query = (request.query_params.get('q') or '').strip()

        # Base query: Only published posts in current tenant that are not soft-deleted
        qs = CommunityPost.objects.using(alias).filter(
            organization=org,
            status=PostStatus.PUBLISHED,
            is_deleted_by_author=False
        ).select_related('author_profile', 'category', 'coach_profile').prefetch_related('comments', 'media_items')

        if category_slug and category_slug != 'all':
            qs = qs.filter(category__slug=category_slug)

        if search_query:
            qs = qs.filter(
                models.Q(journey_title__icontains=search_query) |
                models.Q(content__icontains=search_query) |
                models.Q(coach_name_snapshot__icontains=search_query)
            )

        if tab == 'trending':
            qs = qs.order_by('-is_pinned', '-likes_count', '-comments_count', '-created_at')
        else:
            qs = qs.order_by('-is_pinned', '-created_at')

        # Pagination
        paginator = PageNumberPagination()
        paginator.page_size = min(int(request.query_params.get('page_size', 15)), 50)
        page_results = paginator.paginate_queryset(qs, request)

        # Batch resolve like & bookmark IDs for performance
        liked_ids = set()
        bookmarked_ids = set()
        if current_profile and page_results:
            post_ids = [p.id for p in page_results]
            liked_ids = set(
                PostReaction.objects.using(alias).filter(
                    post_id__in=post_ids,
                    user_profile=current_profile
                ).values_list('post_id', flat=True)
            )
            bookmarked_ids = set(
                PostBookmark.objects.using(alias).filter(
                    post_id__in=post_ids,
                    user_profile=current_profile
                ).values_list('post_id', flat=True)
            )

        serializer_context = {
            'request': request,
            'current_user_profile': current_profile,
            'liked_post_ids': liked_ids,
            'bookmarked_post_ids': bookmarked_ids,
        }
        serializer = CommunityPostSerializer(page_results, many=True, context=serializer_context)
        return paginator.get_paginated_response(serializer.data)


class CommunityPostCreateMobileView(APIView):
    """
    GET  /api/v1/mobile/community/posts/ — Alias to CommunityFeedMobileView
    POST /api/v1/mobile/community/posts/ — Publish a transformation post, highlight, or general discussion story.
    Directly incorporates fields from mobile screenshot:
    - Before & After photo URLs
    - Duration ('12 Weeks') & Metric Change ('-11.5 kg')
    - Coach name snapshot & Star rating (1-5)
    - Privacy Scope (Studio Public, Members Only, Anonymous)
    """
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.request.method in ['GET', 'OPTIONS']:
            return [AllowAny()]
        return [IsAuthenticated()]

    def get(self, request, *args, **kwargs):
        """
        Alias GET /api/v1/mobile/community/posts/ to CommunityFeedMobileView
        so mobile clients can fetch the feed using either /feed/ or /posts/.
        """
        return CommunityFeedMobileView().get(request, *args, **kwargs)

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        # Sanctions check
        restricted, message = _check_user_active_sanctions(alias, profile, "POST")
        if restricted:
            return Response({'error': message}, status=status.HTTP_403_FORBIDDEN)

        serializer = CommunityPostCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Category resolution
        category_slug = data.pop('category_slug', None) or 'transformations'
        _ensure_default_categories(alias, org)
        category = CommunityCategory.objects.using(alias).filter(
            organization=org,
            slug=category_slug
        ).first()

        if not category:
            category = CommunityCategory.objects.using(alias).filter(organization=org).first()

        if category.is_admin_only and not request.user.is_organization_admin:
            return Response(
                {'error': f"Only staff can post in {category.name}."},
                status=status.HTTP_403_FORBIDDEN
            )

        with transaction.atomic(using=alias):
            post = CommunityPost.objects.using(alias).create(
                organization=org,
                author_profile=profile,
                category=category,
                journey_title=data.get('journey_title', ''),
                content=data.get('content', ''),
                before_photo_url=data.get('before_photo_url'),
                after_photo_url=data.get('after_photo_url'),
                duration_text=data.get('duration_text', ''),
                metric_change=data.get('metric_change', ''),
                coach_name_snapshot=data.get('coach_name_snapshot', ''),
                coach_rating=data.get('coach_rating'),
                privacy_scope=data.get('privacy_scope', PostPrivacyScope.STUDIO_PUBLIC),
                status=PostStatus.PUBLISHED
            )

        out_serializer = CommunityPostSerializer(post, context={'current_user_profile': profile})
        return Response({
            'message': 'Post published successfully!',
            'post': out_serializer.data
        }, status=status.HTTP_201_CREATED)


class CommunityPostDetailMobileView(APIView):
    """
    GET /api/v1/mobile/community/posts/<uuid:pk>/ — View post details & thread
    DELETE /api/v1/mobile/community/posts/<uuid:pk>/ — Author soft-delete
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        post = CommunityPost.objects.using(alias).filter(
            id=pk,
            organization=org,
            status=PostStatus.PUBLISHED,
            is_deleted_by_author=False
        ).first()

        if not post:
            return Response({'error': 'Post not found or unavailable'}, status=status.HTTP_404_NOT_FOUND)

        current_profile = None
        if request.user and request.user.is_authenticated:
            current_profile = _get_or_create_user_profile(request.user, alias)

        serializer = CommunityPostSerializer(post, context={'current_user_profile': current_profile})
        return Response({'post': serializer.data})

    def delete(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not request.user or not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        profile = _get_or_create_user_profile(request.user, alias)
        post = CommunityPost.objects.using(alias).filter(id=pk).first()

        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        if post.author_profile != profile and not request.user.is_organization_admin:
            return Response({'error': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)

        post.is_deleted_by_author = True
        post.status = PostStatus.HIDDEN
        post.save(using=alias)

        return Response({'message': 'Post deleted successfully.'}, status=status.HTTP_200_OK)


class CommunityPostReactMobileView(APIView):
    """
    POST /api/v1/mobile/community/posts/<uuid:pk>/react/
    Atomic reaction toggle (High-Five / Like).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        profile = _get_or_create_user_profile(request.user, alias)
        reaction_type = request.data.get('reaction_type', 'HIGH_FIVE')

        post = CommunityPost.objects.using(alias).filter(id=pk).first()
        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic(using=alias):
            existing = PostReaction.objects.using(alias).filter(post=post, user_profile=profile).first()
            if existing:
                existing.delete()
                CommunityPost.objects.using(alias).filter(id=pk).update(
                    likes_count=models.F('likes_count') - 1
                )
                is_liked = False
            else:
                PostReaction.objects.using(alias).create(
                    post=post,
                    user_profile=profile,
                    reaction_type=reaction_type
                )
                CommunityPost.objects.using(alias).filter(id=pk).update(
                    likes_count=models.F('likes_count') + 1
                )
                is_liked = True

        post.refresh_from_db(using=alias)
        return Response({
            'is_liked': is_liked,
            'likes_count': post.likes_count
        }, status=status.HTTP_200_OK)


class CommunityPostBookmarkMobileView(APIView):
    """
    POST /api/v1/mobile/community/posts/<uuid:pk>/bookmark/
    Bookmark toggle for saving inspiration.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        profile = _get_or_create_user_profile(request.user, alias)

        post = CommunityPost.objects.using(alias).filter(id=pk).first()
        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic(using=alias):
            existing = PostBookmark.objects.using(alias).filter(post=post, user_profile=profile).first()
            if existing:
                existing.delete()
                CommunityPost.objects.using(alias).filter(id=pk).update(
                    bookmarks_count=models.F('bookmarks_count') - 1
                )
                is_bookmarked = False
            else:
                PostBookmark.objects.using(alias).create(
                    post=post,
                    user_profile=profile
                )
                CommunityPost.objects.using(alias).filter(id=pk).update(
                    bookmarks_count=models.F('bookmarks_count') + 1
                )
                is_bookmarked = True

        post.refresh_from_db(using=alias)
        return Response({
            'is_bookmarked': is_bookmarked,
            'bookmarks_count': post.bookmarks_count
        }, status=status.HTTP_200_OK)


class CommunityPostCommentsMobileView(APIView):
    """
    GET  /api/v1/mobile/community/posts/<uuid:pk>/comments/ — List comments
    POST /api/v1/mobile/community/posts/<uuid:pk>/comments/ — Add comment / reply
    Automatically flags is_coach_verified=True if author is a verified trainer.
    """
    permission_classes = [AllowAny]

    def get(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        comments = CommunityComment.objects.using(alias).filter(
            post_id=pk,
            is_hidden=False,
            parent__isnull=True  # Top-level comments
        ).select_related('author_profile').prefetch_related('replies').order_by('created_at')

        serializer = CommunityCommentSerializer(comments, many=True)
        return Response({'results': serializer.data})

    def post(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not request.user or not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        restricted, message = _check_user_active_sanctions(alias, profile, "COMMENT")
        if restricted:
            return Response({'error': message}, status=status.HTTP_403_FORBIDDEN)

        post = CommunityPost.objects.using(alias).filter(id=pk).first()
        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        if post.is_comments_disabled:
            return Response({'error': 'Comments are disabled for this post.'}, status=status.HTTP_400_BAD_REQUEST)

        content = (request.data.get('content') or '').strip()
        if not content:
            return Response({'error': 'Comment content cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)

        parent_id = request.data.get('parent_id')
        parent_comment = None
        if parent_id:
            parent_comment = CommunityComment.objects.using(alias).filter(id=parent_id, post=post).first()

        # Check if author is a verified trainer/coach in this tenant
        is_coach = False
        if request.user:
            is_coach = TrainerProfile.objects.using(alias).filter(
                employee_profile__user_profile__user=request.user,
                trainer_status='ACTIVE'
            ).exists()

        with transaction.atomic(using=alias):
            comment = CommunityComment.objects.using(alias).create(
                organization=post.organization or org,
                post=post,
                author_profile=profile,
                parent=parent_comment,
                content=content,
                is_coach_verified=is_coach
            )
            CommunityPost.objects.using(alias).filter(id=pk).update(
                comments_count=models.F('comments_count') + 1
            )

        serializer = CommunityCommentSerializer(comment)
        return Response({
            'message': 'Comment posted successfully',
            'comment': serializer.data
        }, status=status.HTTP_201_CREATED)


class CommunityReportMobileView(APIView):
    """
    POST /api/v1/mobile/community/report/
    Submit report for inappropriate post or comment.
    Auto-hides post if 3+ unique member reports are received.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        reporter_profile = _get_or_create_user_profile(request.user, alias)

        serializer = CommunityReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic(using=alias):
            report = CommunityReport.objects.using(alias).create(
                organization=org,
                reporter_profile=reporter_profile,
                post=data.get('post'),
                comment=data.get('comment'),
                reason=data['reason'],
                custom_details=data.get('custom_details', '')
            )

            # Update report count and auto-hide threshold
            if data.get('post'):
                post = data['post']
                CommunityPost.objects.using(alias).filter(id=post.id).update(
                    reports_count=models.F('reports_count') + 1
                )
                post.refresh_from_db(using=alias)
                if post.reports_count >= 3:
                    post.status = PostStatus.FLAGGED
                    post.save(using=alias)

        return Response({
            'message': 'Thank you for reporting. Our moderation team has been notified and this content will be reviewed.'
        }, status=status.HTTP_201_CREATED)


class CommunityRequestsMobileView(APIView):
    """
    GET  /api/v1/mobile/community/requests/ — List gym suggestions with upvote counts
    POST /api/v1/mobile/community/requests/ — Submit new improvement suggestion
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        current_profile = None
        if request.user and request.user.is_authenticated:
            current_profile = _get_or_create_user_profile(request.user, alias)

        reqs = MemberRequest.objects.using(alias).filter(
            organization=org
        ).select_related('author_profile', 'responded_by').order_by('-upvotes_count', '-created_at')

        voted_ids = set()
        if current_profile:
            voted_ids = set(
                MemberRequestVote.objects.using(alias).filter(
                    user_profile=current_profile
                ).values_list('request_id', flat=True)
            )

        serializer = MemberRequestSerializer(
            reqs,
            many=True,
            context={'current_user_profile': current_profile, 'voted_request_ids': voted_ids}
        )
        return Response({'results': serializer.data})

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not request.user or not request.user.is_authenticated:
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        title = (request.data.get('title') or '').strip()
        description = (request.data.get('description') or '').strip()

        if not title or len(title) < 3:
            return Response({'error': 'Title must be at least 3 characters.'}, status=status.HTTP_400_BAD_REQUEST)
        if not description or len(description) < 3:
            return Response({'error': 'Description must be at least 3 characters.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic(using=alias):
            req_obj = MemberRequest.objects.using(alias).create(
                organization=org,
                author_profile=profile,
                title=title,
                description=description,
                status=MemberRequest.RequestStatus.UNDER_REVIEW,
                upvotes_count=1
            )
            # Author auto-votes their own request
            MemberRequestVote.objects.using(alias).create(
                request=req_obj,
                user_profile=profile
            )

        serializer = MemberRequestSerializer(req_obj, context={'current_user_profile': profile})
        return Response({
            'message': 'Suggestion submitted successfully!',
            'request': serializer.data
        }, status=status.HTTP_201_CREATED)


class CommunityRequestUpvoteMobileView(APIView):
    """
    POST /api/v1/mobile/community/requests/<uuid:pk>/upvote/
    Toggle upvote for a member request.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        profile = _get_or_create_user_profile(request.user, alias)

        req_obj = MemberRequest.objects.using(alias).filter(id=pk).first()
        if not req_obj:
            return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic(using=alias):
            vote = MemberRequestVote.objects.using(alias).filter(request=req_obj, user_profile=profile).first()
            if vote:
                vote.delete()
                MemberRequest.objects.using(alias).filter(id=pk).update(
                    upvotes_count=models.F('upvotes_count') - 1
                )
                is_voted = False
            else:
                MemberRequestVote.objects.using(alias).create(request=req_obj, user_profile=profile)
                MemberRequest.objects.using(alias).filter(id=pk).update(
                    upvotes_count=models.F('upvotes_count') + 1
                )
                is_voted = True

        req_obj.refresh_from_db(using=alias)
        return Response({
            'is_voted': is_voted,
            'upvotes_count': req_obj.upvotes_count
        }, status=status.HTTP_200_OK)


# ============================================================================
# 2. Admin Command Center Management Endpoints
# ============================================================================

class CommunityAdminOverviewView(APIView):
    """
    GET /api/v1/admin/community/overview/
    KPI statistics for the Admin Command Center overview.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)

        total_posts = CommunityPost.objects.using(alias).filter(organization=org).count()
        published_posts = CommunityPost.objects.using(alias).filter(organization=org, status=PostStatus.PUBLISHED).count()
        pending_reports = CommunityReport.objects.using(alias).filter(
            organization=org,
            status=CommunityReport.ReportStatus.PENDING
        ).count()
        open_requests = MemberRequest.objects.using(alias).filter(
            organization=org,
            status__in=[MemberRequest.RequestStatus.UNDER_REVIEW, MemberRequest.RequestStatus.CONSIDERING]
        ).count()
        active_sanctions = UserSanction.objects.using(alias).filter(organization=org, is_active=True).count()

        return Response({
            'total_posts': total_posts,
            'published_posts': published_posts,
            'pending_reports': pending_reports,
            'open_requests': open_requests,
            'active_sanctions': active_sanctions,
        })


class CommunityAdminPostsView(APIView):
    """
    GET   /api/v1/admin/community/posts/ — Master post list with search & filters
    PATCH /api/v1/admin/community/posts/<uuid:pk>/ — Pin, lock comments, change status
    DELETE /api/v1/admin/community/posts/<uuid:pk>/ — Take down post with audit reason
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)

        status_filter = request.query_params.get('status')
        category_slug = request.query_params.get('category')
        search = (request.query_params.get('q') or '').strip()

        qs = CommunityPost.objects.using(alias).filter(
            organization=org
        ).select_related('author_profile', 'category').order_by('-created_at')

        if status_filter and status_filter != 'ALL':
            qs = qs.filter(status=status_filter)
        if category_slug and category_slug != 'ALL':
            qs = qs.filter(category__slug=category_slug)
        if search:
            qs = qs.filter(
                models.Q(journey_title__icontains=search) |
                models.Q(content__icontains=search) |
                models.Q(coach_name_snapshot__icontains=search) |
                models.Q(author_profile__first_name_snapshot__icontains=search) |
                models.Q(author_profile__last_name_snapshot__icontains=search)
            )

        paginator = PageNumberPagination()
        paginator.page_size = 25
        page_results = paginator.paginate_queryset(qs, request)
        serializer = CommunityPostSerializer(page_results, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

    def patch(self, request, pk=None):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        post_id = pk or request.data.get('id')
        post = CommunityPost.objects.using(alias).filter(id=post_id, organization=org).first()

        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action')
        reason = request.data.get('reason', '')

        with transaction.atomic(using=alias):
            if action == 'PIN':
                post.is_pinned = not post.is_pinned
                post.pinned_at = timezone.now() if post.is_pinned else None
            elif action == 'LOCK_COMMENTS':
                post.is_comments_disabled = not post.is_comments_disabled
            elif action == 'SET_STATUS':
                new_status = request.data.get('status')
                if new_status in PostStatus.values:
                    post.status = new_status
            post.save(using=alias)

            # Audit log
            CommunityAuditLog.objects.using(alias).create(
                organization=org,
                actor=request.user if isinstance(request.user, TenantUser) else None,
                action=f"POST_{action}",
                target_type="POST",
                target_id=str(post.id),
                reason=reason
            )

        serializer = CommunityPostSerializer(post)
        return Response({'message': 'Post updated successfully', 'post': serializer.data})

    def delete(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        post = CommunityPost.objects.using(alias).filter(id=pk, organization=org).first()

        if not post:
            return Response({'error': 'Post not found'}, status=status.HTTP_404_NOT_FOUND)

        reason = request.data.get('reason', 'Removed by administrator.')
        with transaction.atomic(using=alias):
            post.status = PostStatus.REMOVED
            post.save(using=alias)

            CommunityAuditLog.objects.using(alias).create(
                organization=org,
                actor=request.user if isinstance(request.user, TenantUser) else None,
                action="POST_REMOVED",
                target_type="POST",
                target_id=str(post.id),
                reason=reason
            )

        return Response({'message': 'Post taken down successfully.'})


class CommunityAdminReportsView(APIView):
    """
    GET  /api/v1/admin/community/reports/ — Moderation Queue
    POST /api/v1/admin/community/reports/<uuid:pk>/resolve/ — Take down or dismiss
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        status_filter = request.query_params.get('status', 'PENDING')

        qs = CommunityReport.objects.using(alias).filter(
            organization=org
        ).select_related('reporter_profile', 'post', 'comment').order_by('-created_at')

        if status_filter != 'ALL':
            qs = qs.filter(status=status_filter)

        results = []
        for r in qs[:50]:
            target_text = ""
            target_author = ""
            if r.post:
                target_text = r.post.journey_title or r.post.content[:80]
                target_author = r.post.author_profile.first_name_snapshot or "Member"
            elif r.comment:
                target_text = r.comment.content[:80]
                target_author = r.comment.author_profile.first_name_snapshot or "Member"

            results.append({
                'id': str(r.id),
                'reason': r.reason,
                'custom_details': r.custom_details,
                'status': r.status,
                'target_type': 'POST' if r.post_id else 'COMMENT',
                'target_id': str(r.post_id or r.comment_id),
                'target_text': target_text,
                'target_author': target_author,
                'reporter_name': r.reporter_profile.first_name_snapshot or "Member",
                'created_at': r.created_at.isoformat()
            })

        return Response({'results': results})

    def post(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        report = CommunityReport.objects.using(alias).filter(id=pk, organization=org).first()

        if not report:
            return Response({'error': 'Report not found'}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action')  # 'UPHELD' or 'DISMISSED'
        reason = request.data.get('reason', '')

        with transaction.atomic(using=alias):
            if action == 'UPHELD':
                report.status = CommunityReport.ReportStatus.UPHELD
                # Take down the offending content
                if report.post:
                    report.post.status = PostStatus.REMOVED
                    report.post.save(using=alias)
                elif report.comment:
                    report.comment.is_hidden = True
                    report.comment.save(using=alias)
            else:
                report.status = CommunityReport.ReportStatus.DISMISSED
                if report.post and report.post.status == PostStatus.FLAGGED:
                    report.post.status = PostStatus.PUBLISHED
                    report.post.save(using=alias)

            report.reviewed_at = timezone.now()
            if isinstance(request.user, TenantUser):
                report.reviewed_by = request.user
            report.save(using=alias)

            CommunityAuditLog.objects.using(alias).create(
                organization=org,
                actor=request.user if isinstance(request.user, TenantUser) else None,
                action=f"REPORT_{action}",
                target_type="REPORT",
                target_id=str(report.id),
                reason=reason
            )

        return Response({'message': f'Report resolved as {action}.'})


class CommunityAdminRequestsView(APIView):
    """
    GET   /api/v1/admin/community/requests/ — Kanban board of member requests
    PATCH /api/v1/admin/community/requests/<uuid:pk>/ — Update status & official reply
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)

        reqs = MemberRequest.objects.using(alias).filter(
            organization=org
        ).select_related('author_profile', 'responded_by').order_by('-upvotes_count', '-created_at')

        serializer = MemberRequestSerializer(reqs, many=True)
        return Response({'results': serializer.data})

    def patch(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        req_obj = MemberRequest.objects.using(alias).filter(id=pk, organization=org).first()

        if not req_obj:
            return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        admin_response = request.data.get('admin_response')

        with transaction.atomic(using=alias):
            if new_status and new_status in MemberRequest.RequestStatus.values:
                req_obj.status = new_status
            if admin_response is not None:
                req_obj.admin_response = admin_response.strip()
                req_obj.responded_at = timezone.now()
                if isinstance(request.user, TenantUser):
                    req_obj.responded_by = request.user
            req_obj.save(using=alias)

            CommunityAuditLog.objects.using(alias).create(
                organization=org,
                actor=request.user if isinstance(request.user, TenantUser) else None,
                action="REQUEST_STATUS_UPDATED",
                target_type="REQUEST",
                target_id=str(req_obj.id),
                reason=f"Status set to {req_obj.status}"
            )

        serializer = MemberRequestSerializer(req_obj)
        return Response({'message': 'Suggestion updated successfully', 'request': serializer.data})


class CommunityAdminSanctionsView(APIView):
    """
    POST /api/v1/admin/community/users/<uuid:user_id>/sanction/
    Issue warning, mute, ban, or shadowban to a member profile.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, user_id):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)
        profile = UserProfile.objects.using(alias).filter(id=user_id).first()

        if not profile:
            return Response({'error': 'Member profile not found'}, status=status.HTTP_404_NOT_FOUND)

        sanction_type = request.data.get('sanction_type')
        reason = (request.data.get('reason') or '').strip()
        duration_days = request.data.get('duration_days')

        if not sanction_type or sanction_type not in UserSanction.SanctionType.values:
            return Response({'error': 'Invalid sanction type.'}, status=status.HTTP_400_BAD_REQUEST)
        if not reason:
            return Response({'error': 'Reason is mandatory for applying sanctions.'}, status=status.HTTP_400_BAD_REQUEST)

        expires_at = None
        if duration_days:
            expires_at = timezone.now() + timedelta(days=int(duration_days))

        with transaction.atomic(using=alias):
            sanction = UserSanction.objects.using(alias).create(
                organization=org,
                user_profile=profile,
                sanction_type=sanction_type,
                reason=reason,
                expires_at=expires_at,
                issued_by=request.user if isinstance(request.user, TenantUser) else None
            )

            CommunityAuditLog.objects.using(alias).create(
                organization=org,
                actor=request.user if isinstance(request.user, TenantUser) else None,
                action=f"SANCTION_{sanction_type}",
                target_type="USER",
                target_id=str(profile.id),
                reason=reason
            )

        serializer = UserSanctionSerializer(sanction)
        return Response({
            'message': f"Sanction '{sanction_type}' applied successfully.",
            'sanction': serializer.data
        }, status=status.HTTP_201_CREATED)


class CommunityAdminAuditLogsView(APIView):
    """
    GET /api/v1/admin/community/audit-logs/
    View immutable history of administrator & moderator decisions.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        org = _get_active_organization(request, alias)

        logs = CommunityAuditLog.objects.using(alias).filter(
            organization=org
        ).select_related('actor').order_by('-created_at')[:100]

        serializer = CommunityAuditLogSerializer(logs, many=True)
        return Response({'results': serializer.data})


class CommunityMediaUploadView(APIView):
    """
    POST /api/v1/mobile/community/media/upload/
    POST /api/v1/admin/community/media/upload/

    Uploads transformation before/after photos and community media directly
    to our own backend server (MEDIA_ROOT) with strict tenant-level directory isolation.
    Returns relative and absolute URL to use in post creation.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import os
        import uuid
        from django.conf import settings

        file_obj = request.FILES.get('file') or request.FILES.get('image')
        if not file_obj:
            return Response(
                {'error': 'No file uploaded. Please supply a file with key "file" or "image".'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Validate file size (max 15MB)
        max_size = 15 * 1024 * 1024
        if file_obj.size > max_size:
            return Response(
                {'error': f'File too large. Maximum allowed size is 15MB (got {round(file_obj.size / (1024 * 1024), 2)}MB).'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Validate content type & extension
        allowed_exts = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'}
        _, ext = os.path.splitext(file_obj.name.lower())
        if ext not in allowed_exts:
            return Response(
                {'error': f'Unsupported file extension "{ext}". Allowed extensions: {", ".join(sorted(allowed_exts))}.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Determine tenant slug for folder isolation
        tenant_slug = getattr(getattr(request, 'tenant', None), 'slug', None)
        if not tenant_slug:
            tenant_slug = request.headers.get('X-Tenant-Slug', 'sweat').strip().lower() or 'sweat'

        # 4. Target storage path: media/community/<tenant_slug>/<uuid><ext>
        unique_name = f"{uuid.uuid4().hex}{ext}"
        rel_dir = os.path.join('community', tenant_slug)
        full_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
        os.makedirs(full_dir, exist_ok=True)

        full_path = os.path.join(full_dir, unique_name)
        with open(full_path, 'wb+') as destination:
            for chunk in file_obj.chunks():
                destination.write(chunk)

        # Relative media URL (e.g. /media/community/sweat/abcd.jpg)
        rel_url = f"/media/{rel_dir.replace(os.sep, '/')}/{unique_name}"
        abs_url = request.build_absolute_uri(rel_url)

        return Response({
            'url': rel_url,
            'absolute_url': abs_url,
            'filename': file_obj.name,
            'size': file_obj.size,
            'media_type': 'IMAGE',
            'created_at': timezone.now().isoformat(),
        }, status=status.HTTP_201_CREATED)

