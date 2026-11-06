import uuid
from django.db import models
from django.utils import timezone


class CMSBanner(models.Model):
    """
    Banners for Mobile App Home Screen, category headers, and promo placements.
    Supports single or carousel image/video displays with deep linking.
    """
    PLACEMENT_CHOICES = [
        ('HOME_HERO', 'Home Screen Hero Banner'),
        ('HOME_PROMO', 'Home Screen Promo Card'),
        ('CATEGORY_HEADER', 'Category Header Banner'),
        ('OFFERS_SLIDER', 'Offers & Discounts Slider'),
    ]

    MEDIA_TYPE_CHOICES = [
        ('IMAGE', 'Image'),
        ('VIDEO', 'Video'),
    ]

    LINK_TYPE_CHOICES = [
        ('NONE', 'None'),
        ('CATEGORY', 'Open Program Category'),
        ('PACKAGE', 'Open Membership Package'),
        ('BOOKING', 'Open Class Booking'),
        ('EXTERNAL_URL', 'External Web Link'),
        ('SCREEN', 'App Screen Route'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.CASCADE, related_name='cms_banners'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cms_banners', help_text='Null means all branches'
    )
    title = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=300, blank=True, default='')
    placement = models.CharField(max_length=50, choices=PLACEMENT_CHOICES, default='HOME_HERO')
    media_type = models.CharField(max_length=20, choices=MEDIA_TYPE_CHOICES, default='IMAGE')
    
    # Media URLs
    media_url = models.TextField(help_text='Direct URL for Image or Video stream')
    thumbnail_url = models.TextField(blank=True, default='', help_text='Poster image for videos or fallback')
    media_file = models.ForeignKey(
        'File', on_delete=models.SET_NULL, null=True, blank=True, related_name='banner_media'
    )

    # Tap action / deep linking
    link_type = models.CharField(max_length=50, choices=LINK_TYPE_CHOICES, default='NONE')
    link_target = models.CharField(max_length=500, blank=True, default='', help_text='ID, slug, or URL depending on link_type')

    display_order = models.IntegerField(default=0, help_text='Sorting sequence')
    is_active = models.BooleanField(default=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_banners'
    )

    class Meta:
        db_table = 'cms_banners'
        ordering = ['display_order', '-created_at']

    def __str__(self):
        return f"{self.title} ({self.placement})"


class ProgramCategoryMedia(models.Model):
    """
    Dedicated media and content descriptors for Program Categories (e.g., Pilates, Bootcamp).
    Contains icon for tabs/chips, hero banner, preview video, and description.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.CASCADE, related_name='category_cms_media'
    )
    program_category = models.ForeignKey(
        'ProgramCategory', on_delete=models.CASCADE, null=True, blank=True, related_name='cms_media'
    )
    class_category = models.ForeignKey(
        'ClassCategory', on_delete=models.CASCADE, null=True, blank=True, related_name='cms_media'
    )
    category_name = models.CharField(max_length=150, help_text='e.g., Sweat Pilates, Sweat Bootcamp')
    category_code = models.CharField(max_length=100, blank=True, default='')

    # Dedicated visual assets
    icon_url = models.TextField(blank=True, default='', help_text='Small square/circle icon for navigation & pills')
    banner_url = models.TextField(blank=True, default='', help_text='Wide hero banner image for category header')
    content_description = models.TextField(blank=True, default='', help_text='Rich marketing description of the category')
    video_url = models.TextField(blank=True, default='', help_text='Preview/teaser video URL')
    showcase_images = models.JSONField(default=list, blank=True, help_text='Gallery of showcase image URLs')

    tagline = models.CharField(max_length=200, blank=True, default='')
    display_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cms_program_category_media'
        ordering = ['display_order', 'category_name']

    def __str__(self):
        return f"Media: {self.category_name}"


class CMSMediaAsset(models.Model):
    """
    Media Library for images, audio tracks (workout music, guided meditations),
    and video clips (trailers, form guides) for the app.
    """
    MEDIA_TYPE_CHOICES = [
        ('IMAGE', 'Image'),
        ('AUDIO', 'Audio Track'),
        ('VIDEO', 'Video Clip'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.CASCADE, related_name='cms_media_assets'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    media_type = models.CharField(max_length=20, choices=MEDIA_TYPE_CHOICES)
    
    media_url = models.TextField()
    thumbnail_url = models.TextField(blank=True, default='')
    
    media_file = models.ForeignKey(
        'File', on_delete=models.SET_NULL, null=True, blank=True, related_name='cms_assets'
    )

    duration_seconds = models.IntegerField(null=True, blank=True, help_text='For audio or video')
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=100, blank=True, default='')
    tags = models.JSONField(default=list, blank=True)

    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    uploaded_by = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_cms_media'
    )

    class Meta:
        db_table = 'cms_media_assets'
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.media_type}] {self.title}"
