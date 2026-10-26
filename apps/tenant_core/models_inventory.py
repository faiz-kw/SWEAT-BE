"""
apps/tenant_core/models_inventory.py — Retail Products, Merch, Supplements & Pro Shop Inventory
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch


class Supplier(models.Model):
    """
    Suppliers, manufacturers, equipment vendors, and nutrition distributors.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='suppliers'
    )
    name = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=100, default='General Equipment', blank=True)
    contact_person = models.CharField(max_length=150, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    address = models.TextField(blank=True, default='')
    lead_time_days = models.PositiveIntegerField(default=7, help_text="Typical fulfillment lead time in days")
    payment_terms = models.CharField(max_length=100, default='Net 30', blank=True)
    status = models.CharField(max_length=30, default='Active', choices=[('Active', 'Active'), ('Inactive', 'Inactive')])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'inventory_suppliers'
        ordering = ['name']
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_supp_org_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.category})"


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
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name='products',
        help_text="Optional supplier / vendor providing this product"
    )
    name = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES, default='Supplements', db_index=True)
    product_type = models.CharField(max_length=50, choices=PRODUCT_TYPE_CHOICES, default='Physical Goods')
    sku = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Retail selling price")
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Cost / wholesale price")
    stock_quantity = models.IntegerField(default=0, help_text="Current available inventory units")
    
    # Batch & Expiry tracking
    batch_number = models.CharField(max_length=100, blank=True, null=True, help_text="Batch / Lot number for batch tracking")
    expiry_date = models.DateField(blank=True, null=True, help_text="Expiration date for perishable inventory")
    manufacturing_date = models.DateField(blank=True, null=True, help_text="Manufacturing date")

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

    def get_available_stock(self, using=None):
        from django.db.models import Sum
        qs = self.reservations.all()
        if using:
            qs = qs.using(using)
        active_reserved = qs.filter(
            status='PENDING',
            expires_at__gt=timezone.now()
        ).aggregate(total=Sum('quantity'))['total'] or 0
        return max(0, self.stock_quantity - active_reserved)

    def __str__(self):
        return f"{self.name} ({self.category} - ₹{self.price})"


class StockReservation(models.Model):
    """
    Durable stock reservation held for active pending retail orders.
    Prevents overselling during the checkout and payment verification window.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending Payment'),
        ('CONFIRMED', 'Confirmed Sale'),
        ('RELEASED', 'Released / Expired / Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='reservations'
    )
    order = models.ForeignKey(
        'Order', on_delete=models.CASCADE, related_name='stock_reservations'
    )
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    reserved_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'inventory_stock_reservations'
        indexes = [
            models.Index(fields=['product', 'status', 'expires_at'], name='idx_stkres_prod_st_exp'),
            models.Index(fields=['order', 'status'], name='idx_stkres_ord_st'),
        ]

    def __str__(self):
        return f"Reservation {self.id}: {self.quantity}x {self.product.name} [{self.status}]"


class InventoryMovement(models.Model):
    """
    Authoritative audit ledger for physical and reserved stock lifecycle movements.
    """
    MOVEMENT_TYPE_CHOICES = [
        ('RESERVATION', 'Stock Reserved'),
        ('RESERVATION_RELEASE', 'Reservation Released'),
        ('SALE', 'Sale Deducted'),
        ('RESTOCK', 'Manual Restock'),
        ('ADJUSTMENT', 'Stock Adjustment'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='movements'
    )
    order = models.ForeignKey(
        'Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_movements'
    )
    movement_type = models.CharField(max_length=30, choices=MOVEMENT_TYPE_CHOICES, db_index=True)
    quantity_change = models.IntegerField(help_text="Negative for deductions/sales, positive for restocks/releases")
    balance_after = models.IntegerField(help_text="Stock quantity after movement")
    notes = models.CharField(max_length=255, null=True, blank=True)
    actor_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_movements'
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = 'inventory_movements'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['product', 'created_at'], name='idx_invmov_prod_dt'),
            models.Index(fields=['order'], name='idx_invmov_order'),
        ]

    def __str__(self):
        return f"{self.movement_type}: {self.product.name} ({self.quantity_change:+d}) -> {self.balance_after}"

