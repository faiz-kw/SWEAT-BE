import uuid
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from django.utils import timezone
from django.db.models import Q

from apps.tenant_core.permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from apps.tenant_core.context import get_current_tenant_db_alias
from apps.tenant_core.models_cms import CMSBanner, ProgramCategoryMedia, CMSMediaAsset
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_catalog import ProgramCategory
from apps.tenant_core.models_classes import ClassCategory
from apps.tenant_core.serializers_cms import (
    CMSBannerSerializer, ProgramCategoryMediaSerializer, CMSMediaAssetSerializer
)


class CMSBannerViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for home screen & promo banners.
    """
    serializer_class = CMSBannerSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'cms'
    required_submodule = 'banners'
    required_permission = 'cms.banners.view'
    permission_action_map = {
        'create': 'cms.banners.manage',
        'update': 'cms.banners.manage',
        'partial_update': 'cms.banners.manage',
        'destroy': 'cms.banners.manage',
        'reorder': 'cms.banners.manage',
        'upload_media': 'cms.banners.manage',
    }

    def get_queryset(self):
        alias = get_current_tenant_db_alias() or 'default'
        qs = CMSBanner.objects.using(alias).select_related('branch', 'organization').all()
        placement = self.request.query_params.get('placement')
        if placement:
            qs = qs.filter(placement=placement)
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ('true', '1'))
        return qs

    def perform_create(self, serializer):
        alias = get_current_tenant_db_alias() or 'default'
        org = getattr(self.request, 'organization', None) or Organization.objects.using(alias).filter(status='ACTIVE').first()
        user = getattr(self.request, 'user', None)
        tenant_user = getattr(user, 'tenant_user', None) if user else None
        serializer.save(organization=org, created_by=tenant_user)

    @action(detail=False, methods=['post'], url_path='reorder')
    def reorder(self, request):
        """
        Bulk update display orders: payload = [{"id": "<uuid>", "display_order": 0}, ...]
        """
        alias = get_current_tenant_db_alias() or 'default'
        items = request.data if isinstance(request.data, list) else request.data.get('items', [])
        for item in items:
            b_id = item.get('id')
            order = item.get('display_order', 0)
            if b_id:
                CMSBanner.objects.using(alias).filter(id=b_id).update(display_order=order)
        return Response({'status': 'reordered', 'count': len(items)})

    @action(detail=False, methods=['post'], url_path='upload-media')
    def upload_media(self, request):
        """
        Accepts a direct file upload (video or image) for CMS banners.
        Saves the file to media storage and returns the stream/CDN URL.
        """
        uploaded = request.FILES.get('file') or request.FILES.get('media_file')
        if not uploaded:
            return Response({'error': 'No file provided. Expected field "file".'}, status=status.HTTP_400_BAD_REQUEST)

        import os, re, uuid, mimetypes
        from pathlib import Path
        from django.conf import settings
        from apps.tenant_core.models_infra import File

        alias = get_current_tenant_db_alias() or 'default'
        org = getattr(request, 'organization', None) or Organization.objects.using(alias).filter(status='ACTIVE').first()
        org_id = str(org.id) if org else 'global'

        orig_name = getattr(uploaded, 'name', 'media_file')
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(orig_name))
        unique_prefix = uuid.uuid4().hex[:8]
        filename = f"{unique_prefix}_{safe_name}"

        media_root = getattr(settings, 'MEDIA_ROOT', None) or (Path(settings.BASE_DIR) / 'media')
        target_dir = Path(media_root) / 'cms_banners' / org_id
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / filename

        total_bytes = 0
        with open(file_path, 'wb') as out_f:
            if hasattr(uploaded, 'chunks'):
                for chunk in uploaded.chunks():
                    out_f.write(chunk)
                    total_bytes += len(chunk)
            else:
                data = uploaded.read()
                out_f.write(data)
                total_bytes += len(data)

        # Detect media type
        content_type = getattr(uploaded, 'content_type', '') or mimetypes.guess_type(orig_name)[0] or ''
        is_video = 'video' in content_type.lower() or any(orig_name.lower().endswith(ext) for ext in ('.mp4', '.mov', '.webm', '.m4v', '.mkv', '.avi'))
        media_type = 'VIDEO' if is_video else 'IMAGE'

        media_url = f"/media/cms_banners/{org_id}/{filename}"

        file_rec = None
        try:
            user = getattr(request, 'user', None)
            tenant_user = getattr(user, 'tenant_user', None) if user else None
            file_rec = File.objects.using(alias).create(
                organization=org,
                file_name=orig_name,
                file_size=total_bytes,
                mime_type=content_type or ('video/mp4' if is_video else 'image/jpeg'),
                storage_provider='LOCAL',
                object_key=f"cms_banners/{org_id}/{filename}",
                uploaded_by=tenant_user,
            )
        except Exception:
            pass

        return Response({
            'url': media_url,
            'file_name': orig_name,
            'file_size': total_bytes,
            'media_type': media_type,
            'file_id': str(file_rec.id) if file_rec else None,
        }, status=status.HTTP_201_CREATED)


class ProgramCategoryMediaViewSet(viewsets.ModelViewSet):
    """
    Admin management for Program Category Visual Assets (Pilates, Bootcamp).
    """
    serializer_class = ProgramCategoryMediaSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'cms'
    required_submodule = 'categories'
    required_permission = 'cms.categories.view'
    permission_action_map = {
        'create': 'cms.categories.manage',
        'update': 'cms.categories.manage',
        'partial_update': 'cms.categories.manage',
        'destroy': 'cms.categories.manage',
        'sync_categories': 'cms.categories.manage',
    }

    def get_queryset(self):
        alias = get_current_tenant_db_alias() or 'default'
        return ProgramCategoryMedia.objects.using(alias).select_related(
            'program_category', 'class_category', 'organization'
        ).all()

    def perform_create(self, serializer):
        alias = get_current_tenant_db_alias() or 'default'
        org = getattr(self.request, 'organization', None) or Organization.objects.using(alias).filter(status='ACTIVE').first()
        serializer.save(organization=org)

    @action(detail=False, methods=['post'], url_path='sync-categories')
    def sync_categories(self, request):
        """
        Auto-populates ProgramCategoryMedia entries for any existing ClassCategory or ProgramCategory
        (e.g., Sweat Pilates, Sweat Bootcamp) that do not yet have CMS records.
        """
        alias = get_current_tenant_db_alias() or 'default'
        org = getattr(self.request, 'organization', None) or Organization.objects.using(alias).filter(status='ACTIVE').first()
        
        created_count = 0
        # 1. Check ClassCategory
        class_cats = ClassCategory.objects.using(alias).all()
        for cc in class_cats:
            existing = ProgramCategoryMedia.objects.using(alias).filter(
                Q(class_category=cc) | Q(category_name__iexact=cc.name)
            ).first()
            if not existing:
                ProgramCategoryMedia.objects.using(alias).create(
                    organization=org,
                    class_category=cc,
                    category_name=cc.name,
                    category_code=cc.name.upper().replace(' ', '_'),
                    tagline=f"Experience the transformative power of {cc.name}",
                    content_description=cc.description or f"Comprehensive training program for {cc.name}.",
                    is_active=True
                )
                created_count += 1

        # 2. Check ProgramCategory
        prog_cats = ProgramCategory.objects.using(alias).all()
        for pc in prog_cats:
            existing = ProgramCategoryMedia.objects.using(alias).filter(
                Q(program_category=pc) | Q(category_name__iexact=pc.name)
            ).first()
            if not existing:
                ProgramCategoryMedia.objects.using(alias).create(
                    organization=org,
                    program_category=pc,
                    category_name=pc.name,
                    category_code=pc.code or pc.name.upper().replace(' ', '_'),
                    tagline=f"Empowering health with {pc.name}",
                    content_description=pc.description or f"Official curriculum for {pc.name}.",
                    is_active=True
                )
                created_count += 1

        return Response({
            'status': 'synced',
            'created_count': created_count,
            'total_categories': ProgramCategoryMedia.objects.using(alias).count()
        })


class CMSMediaAssetViewSet(viewsets.ModelViewSet):
    """
    Admin Media Library for Images, Audio, and Video files.
    """
    serializer_class = CMSMediaAssetSerializer
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'cms'
    required_submodule = 'media'
    required_permission = 'cms.media.view'
    permission_action_map = {
        'create': 'cms.media.upload',
        'update': 'cms.media.upload',
        'partial_update': 'cms.media.upload',
        'destroy': 'cms.media.delete',
    }

    def get_queryset(self):
        alias = get_current_tenant_db_alias() or 'default'
        qs = CMSMediaAsset.objects.using(alias).select_related('uploaded_by', 'organization').all()
        media_type = self.request.query_params.get('media_type')
        if media_type:
            qs = qs.filter(media_type=media_type.upper())
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))
        return qs

    def perform_create(self, serializer):
        alias = get_current_tenant_db_alias() or 'default'
        org = getattr(self.request, 'organization', None) or Organization.objects.using(alias).filter(status='ACTIVE').first()
        user = getattr(self.request, 'user', None)
        tenant_user = getattr(user, 'tenant_user', None) if user else None
        serializer.save(organization=org, uploaded_by=tenant_user)


class MobileCMSHomeFeedView(APIView):
    """
    High-performance API for Mobile App Home Screen to consume:
    - Active Hero Banners & Promo Banners
    - Program Categories with Icons, Banners & Descriptions (Pilates, Bootcamp)
    - Featured Media Assets (Videos & Audio)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        # Public endpoint: resolve the tenant DB the same way the other mobile views do,
        # so CMS tables are read from the tenant database (not the empty 'default').
        from apps.tenant_core.views_mobile import _resolve_mobile_tenant_and_db
        alias, _tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = get_current_tenant_db_alias() or 'default'
        now = timezone.now()
        branch_id = request.query_params.get('branch_id')

        # 1. Banners
        banners_qs = CMSBanner.objects.using(alias).filter(
            is_active=True
        ).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=now)
        ).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=now)
        )
        if branch_id:
            banners_qs = banners_qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        else:
            banners_qs = banners_qs.filter(branch__isnull=True)

        hero_banners = CMSBannerSerializer(banners_qs.filter(placement='HOME_HERO'), many=True).data
        promo_cards = CMSBannerSerializer(banners_qs.filter(placement='HOME_PROMO'), many=True).data
        offers_slider = CMSBannerSerializer(banners_qs.filter(placement='OFFERS_SLIDER'), many=True).data

        # 2. Categories
        categories_qs = ProgramCategoryMedia.objects.using(alias).filter(is_active=True).order_by('display_order')
        categories_data = ProgramCategoryMediaSerializer(categories_qs, many=True).data

        # 3. Featured Media (e.g. video reels or meditation audio)
        featured_media_qs = CMSMediaAsset.objects.using(alias).filter(is_published=True).order_by('-created_at')[:10]
        media_data = CMSMediaAssetSerializer(featured_media_qs, many=True).data

        return Response({
            'banners': {
                'hero': hero_banners,
                'promo': promo_cards,
                'offers': offers_slider,
            },
            'program_categories': categories_data,
            'featured_media': media_data,
            'timestamp': now.isoformat(),
        })
