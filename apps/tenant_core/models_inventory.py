"""
apps/tenant_core/models_inventory.py — Retail Products, Merch, Supplements & Pro Shop Inventory
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch


class Product(models.Model):
    """
    Master product catalog item for gym pro shop, merchandise, supplements, and digital goods.
    Fully visible and editable in Django Admin and accessible via REST API.
    """
    CATEGORY_CHOICES = [
        ('Supplements', 'Supplements'),
        ('Apparel', 'Apparel'),
        ('Accessories', 'Accessories'),
        ('Equipment', 'Equipment'),
        ('Beverages', 'Beverages'),
        ('Digital Guides', 'Digital Guides'),
        ('Snacks', 'Snacks'),
        ('Other', 'Other'),
    ]
    PRODUCT_TYPE_CHOICES = [
        ('Physical Goods', 'Physical Goods'),
        ('Digital Download', 'Digital Download'),
        ('Supplement / Nutrition', 'Supplement / Nutrition'),
    ]
    STATUS_CHOICES = [
        ('In Stock', 'In Stock'),
        ('Low Stock', 'Low Stock'),
        ('Out of Stock', 'Out of Stock'),
        ('Discontinued', 'Discontinued'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='products'
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='branch_products',
        help_text="Optional specific branch allocation. If null, product is catalog-wide."
    )
    name = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES, default='Supplements', db_index=True)
    product_type = models.CharField(max_length=50, choices=PRODUCT_TYPE_CHOICES, default='Physical Goods')
    sku = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Retail selling price")
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Cost / wholesale price")
    stock_quantity = models.IntegerField(default=0, help_text="Current available inventory units")
    
    # Media
    image_url = models.TextField(blank=True, null=True, help_text="Primary display image URL or Base64")
    images = models.JSONField(default=list, blank=True, help_text="List of multiple product gallery images")
    video_url = models.TextField(blank=True, null=True, help_text="Product video link or file URL")
    
    description = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='In Stock')
    is_active = models.BooleanField(default=True, db_index=True)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'retail_products'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'is_active'], name='idx_prod_org_active'),
            models.Index(fields=['category', 'status'], name='idx_prod_cat_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.category} - ₹{self.price})"
