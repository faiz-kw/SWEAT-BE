"""
backend/scripts/seed_community_data.py
Seeds default categories and initial realistic community posts matching the mobile developer's designs
across all active organizations in the tenant database.
"""

import os
import sys
import uuid
from decimal import Decimal
from django.utils import timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.master.models import Tenant
from apps.authentication.views import _register_and_resolve_tenant
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile, TrainerProfile
from apps.tenant_core.models_community import (
    CommunityCategory, CommunityPost, CommunityComment,
    PostReaction, PostBookmark, CommunityReport, MemberRequest,
    MemberRequestVote, CommunityAuditLog,
    PostPrivacyScope, PostStatus
)


def seed_for_organization(alias, org):
    print(f"\n[INFO] Seeding Community for Organization: {org.name} ({org.id})")

    # 1. Categories
    cat_transformations, _ = CommunityCategory.objects.using(alias).get_or_create(
        organization=org,
        slug='transformations',
        defaults={
            'name': 'Transformations',
            'description': 'Real member transformation journeys, before/after photos, and fitness milestones.',
            'icon_name': 'flame',
            'color_hex': '#E11D48',
            'display_order': 1,
            'is_active': True,
        }
    )

    cat_highlights, _ = CommunityCategory.objects.using(alias).get_or_create(
        organization=org,
        slug='highlights',
        defaults={
            'name': 'Highlights',
            'description': 'Workout PRs, studio achievements, and daily milestones.',
            'icon_name': 'sparkles',
            'color_hex': '#2563EB',
            'display_order': 2,
            'is_active': True,
        }
    )

    cat_discussions, _ = CommunityCategory.objects.using(alias).get_or_create(
        organization=org,
        slug='discussions',
        defaults={
            'name': 'Discussions',
            'description': 'Tips, nutrition advice, questions, and open member community discussions.',
            'icon_name': 'message-circle',
            'color_hex': '#10B981',
            'display_order': 3,
            'is_active': True,
        }
    )

    cat_announcements, _ = CommunityCategory.objects.using(alias).get_or_create(
        organization=org,
        slug='announcements',
        defaults={
            'name': 'Announcements',
            'description': 'Official studio notices, schedule updates, and trainer announcements.',
            'icon_name': 'bell',
            'color_hex': '#F59E0B',
            'display_order': 4,
            'is_admin_only': True,
            'is_active': True,
        }
    )

    # 2. Member & Coach Profiles
    user_anurupa = TenantUser.objects.using(alias).filter(email=f'anurupa@{org.code.lower() or "sweat"}.test').first()
    if not user_anurupa:
        user_anurupa = TenantUser.objects.using(alias).create(
            organization=org,
            email=f'anurupa@{org.code.lower() or "sweat"}.test',
            first_name='Anurupa',
            last_name='Bhattacharjee',
            status='ACTIVE',
            user_type='CUSTOMER'
        )

    profile_anurupa, _ = UserProfile.objects.using(alias).get_or_create(
        user=user_anurupa,
        defaults={
            'first_name_snapshot': 'Anurupa',
            'last_name_snapshot': 'Bhattacharjee',
            'member_status': 'ACTIVE',
            'member_type': 'MEMBER'
        }
    )

    user_zahid = TenantUser.objects.using(alias).filter(email=f'zahid.coach@{org.code.lower() or "sweat"}.test').first()
    if not user_zahid:
        user_zahid = TenantUser.objects.using(alias).create(
            organization=org,
            email=f'zahid.coach@{org.code.lower() or "sweat"}.test',
            first_name='Zahid',
            last_name='Hussain',
            status='ACTIVE',
            user_type='STAFF'
        )

    profile_zahid, _ = UserProfile.objects.using(alias).get_or_create(
        user=user_zahid,
        defaults={
            'first_name_snapshot': 'Zahid',
            'last_name_snapshot': 'Hussain',
            'member_status': 'ACTIVE',
            'member_type': 'MEMBER'
        }
    )

    # 3. Canonical Transformation Post
    post_trans, created_trans = CommunityPost.objects.using(alias).get_or_create(
        organization=org,
        journey_title='PCOD & Core Strength Journey',
        defaults={
            'author_profile': profile_anurupa,
            'category': cat_transformations,
            'content': f'Three years of failing, rising, falling, being consistent, inconsistent, and fighting my way through severe PCOD and metabolic hurdles. With custom Pilates reformer training and strength conditioning at {org.name}, I finally reversed my symptoms, dropped 11.5 kg, and built real functional core strength. To anyone struggling: don’t quit!',
            'before_photo_url': 'https://images.unsplash.com/photo-1518611012118-696072aa579a?auto=format&fit=crop&w=800&q=80',
            'after_photo_url': 'https://images.unsplash.com/photo-1540497077202-7c8a3999166f?auto=format&fit=crop&w=800&q=80',
            'duration_text': '12 Weeks',
            'metric_change': '-11.5 kg',
            'coach_name_snapshot': 'Shweta Arora Chawla',
            'coach_rating': Decimal('5.0'),
            'privacy_scope': PostPrivacyScope.STUDIO_PUBLIC,
            'status': PostStatus.PUBLISHED,
            'likes_count': 14,
            'comments_count': 4,
            'bookmarks_count': 7
        }
    )

    # Coach Comment
    if created_trans or not post_trans.comments.filter(author_profile=profile_zahid).exists():
        CommunityComment.objects.using(alias).create(
            organization=org,
            post=post_trans,
            author_profile=profile_zahid,
            content=f'🙌🙌🙌🙌 Super proud of your dedication Anurupa! Keep inspiring the entire {org.name} community.',
            is_coach_verified=True,
            likes_count=5
        )

    # 4. Official Studio Announcement
    CommunityPost.objects.using(alias).get_or_create(
        organization=org,
        journey_title='Studio Renovation & New Reformer Machines Arriving!',
        defaults={
            'author_profile': profile_zahid,
            'category': cat_announcements,
            'content': 'We are thrilled to announce that 4 brand new Allegro 2 Reformer towers are arriving this Saturday for Studio 1. Expect enhanced suspension tracks and new weekend masterclasses with Coach Shweta!',
            'privacy_scope': PostPrivacyScope.STUDIO_PUBLIC,
            'status': PostStatus.PUBLISHED,
            'is_pinned': True,
            'likes_count': 32,
            'comments_count': 8,
            'bookmarks_count': 12
        }
    )

    # 5. Member Requests (Voice Your Idea)
    MemberRequest.objects.using(alias).get_or_create(
        organization=org,
        title='Add Sunday Evening 6:30 PM Reformer Pilates Slot',
        defaults={
            'author_profile': profile_anurupa,
            'description': 'A lot of working professionals would really appreciate a Sunday evening session to wrap up the weekend before the work week starts.',
            'status': MemberRequest.RequestStatus.PLANNED,
            'upvotes_count': 23,
            'admin_response': 'Great news! We have scheduled a trial 6:30 PM Reformer slot starting next Sunday with Coach Zahid.',
            'responded_at': timezone.now()
        }
    )

    MemberRequest.objects.using(alias).get_or_create(
        organization=org,
        title='Cold Brew / Electrolyte Station in Studio Lounge',
        defaults={
            'author_profile': profile_anurupa,
            'description': 'Post-bootcamp electrolyte refills or cold brew tap would be awesome for recovery.',
            'status': MemberRequest.RequestStatus.UNDER_REVIEW,
            'upvotes_count': 15
        }
    )

    # 6. Sample Pending Moderation Report for Admin Review Queue
    if not CommunityReport.objects.using(alias).filter(organization=org, status=CommunityReport.ReportStatus.PENDING).exists():
        CommunityReport.objects.using(alias).create(
            organization=org,
            reporter_profile=profile_zahid,
            post=post_trans,
            reason=CommunityReport.ReportReason.OFF_TOPIC,
            custom_details='Flagged for standard review: Please verify user transformation verification tag.',
            status=CommunityReport.ReportStatus.PENDING
        )

    # 7. Initial Audit Log
    admin_user = TenantUser.objects.using(alias).filter(organization=org).first()
    if not CommunityAuditLog.objects.using(alias).filter(organization=org).exists():
        CommunityAuditLog.objects.using(alias).create(
            organization=org,
            actor=admin_user,
            action='COMMUNITY_INITIALIZED',
            target_type='SYSTEM',
            target_id=str(org.id),
            reason=f'Community module enabled and initial guidelines configured for {org.name}'
        )

    print(f"[SUCCESS] Seeded categories, posts, requests, report, and audit log for {org.name}")


def run():
    print("[INFO] Seeding Community data for active tenants...")
    tenants = Tenant.objects.using('default').filter(status='ACTIVE')
    if not tenants.exists():
        print("[ERROR] No active tenants found.")
        return

    for tenant in tenants:
        print(f"\nProcessing tenant: {tenant.name} ({tenant.slug})")
        alias = _register_and_resolve_tenant(tenant)
        set_tenant_db_alias(alias)

        orgs = list(Organization.objects.using(alias).filter(status='ACTIVE'))
        if not orgs:
            orgs = list(Organization.objects.using(alias).all())

        for org in orgs:
            seed_for_organization(alias, org)

    print("\n[COMPLETE] Community dataset seeded across all tenant organizations successfully!")


if __name__ == '__main__':
    run()
