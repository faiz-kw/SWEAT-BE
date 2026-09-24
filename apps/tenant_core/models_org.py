"""
Dedicated Tenant DB — Organization & Branch Hierarchy Models (4 Tables)
Tables: organizations, company_entities, locations, branches

These models live in every tenant's dedicated PostgreSQL database.
They are never stored in the Master DB.
"""

import uuid
from django.db import models
from django.utils import timezone


class Organization(models.Model):
    """
    The top-level business organization inside a tenant's dedicated database.
    Created and managed by the platform team. One per tenant (typically).
    Tenant Org Admins can view but cannot structurally create/deactivate it.
    """
    STATUS = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True, help_text='Stable organization code')
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250, blank=True, null=True)
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=30, blank=True, default='')
    country = models.CharField(max_length=10, default='IN')
    currency = models.CharField(max_length=10, default='INR')
    timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    status = models.CharField(max_length=30, choices=STATUS, default='DRAFT')
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'organizations'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.status}]"


class CompanyEntity(models.Model):
    """
    Optional legal/company entities under the organization.
    e.g. 'Elevate Fitness Pvt Ltd' (GST entity) vs 'Elevate Wellness LLP' (another entity).
    Created/managed by platform team.
    """
    STATUS = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
        ('CLOSED', 'Closed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='company_entities')
    code = models.CharField(max_length=50, help_text='Stable code, unique per organization')
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=250)
    registration_number = models.CharField(max_length=100, blank=True, default='', help_text='CIN/Company Registration')
    tax_registration_number = models.CharField(max_length=100, blank=True, default='', help_text='GSTIN / PAN')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=30, blank=True, default='')
    address = models.TextField(blank=True, default='')
    status = models.CharField(max_length=30, choices=STATUS, default='DRAFT')
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'company_entities'
        unique_together = ('organization', 'code')
        ordering = ['name']
        verbose_name_plural = 'Company entities'

    def save(self, *args, **kwargs):
        if not self.legal_name and self.name:
            self.legal_name = self.name
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class Location(models.Model):
    """
    Geographical/business areas (e.g. 'Goregaon West', 'Andheri East').
    Represents a geographic zone that can contain multiple branches.
    Created/managed by platform team.
    """
    STATUS = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='locations')
    code = models.CharField(max_length=50, help_text='Stable code, unique per organization')
    name = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    area = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    country = models.CharField(max_length=10, default='IN')
    postal_code = models.CharField(max_length=20, blank=True, default='')
    status = models.CharField(max_length=30, choices=STATUS, default='ACTIVE')
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'locations'
        unique_together = ('organization', 'code')
        ordering = ['name']

    def __str__(self):
        return f"{self.name}, {self.city}"


class Branch(models.Model):
    """
    Actual operational gyms/studios/centres (e.g. 'Goregaon Studio 1', 'Andheri Pilates Centre').
    The leaf node of the hierarchy. This is what staff and members belong to.
    Created/managed by platform team.
    """
    STATUS = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('SUSPENDED', 'Suspended'),
        ('CLOSED', 'Closed'),
        ('TEMPORARILY_CLOSED', 'Temporarily Closed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.RESTRICT, related_name='branches')
    location = models.ForeignKey(Location, on_delete=models.RESTRICT, related_name='branches')
    company_entity = models.ForeignKey(
        CompanyEntity, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='branches', help_text='Optional: which legal entity operates this branch'
    )
    code = models.CharField(max_length=50, help_text='Stable code, unique per organization')
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True, default='')
    address_line_1 = models.CharField(max_length=250, blank=True, null=True)
    address_line_2 = models.CharField(max_length=250, blank=True, null=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    geofence_radius_meters = models.IntegerField(default=200, help_text='Maximum allowed distance in meters for attendance check-in')
    geofence_enforcement = models.CharField(
        max_length=20,
        default='STRICT',
        choices=[('STRICT', 'Strict Lock'), ('FLAG_AUDIT', 'Audit Flag Only')],
        help_text='Strictly block attendance or flag for audit when outside radius'
    )
    timezone = models.CharField(max_length=100, default='Asia/Kolkata')
    phone = models.CharField(max_length=30, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    capacity = models.IntegerField(default=0, help_text='Max concurrent members / floor capacity')
    floor_area_sqft = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    business_open_time = models.CharField(max_length=10, blank=True, default='06:00')
    business_close_time = models.CharField(max_length=10, blank=True, default='22:00')
    is_passport_eligible = models.BooleanField(default=False, help_text='Cross-branch member access allowed')
    status = models.CharField(max_length=30, choices=STATUS, default='ACTIVE')
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'branches'
        unique_together = ('organization', 'code')
        ordering = ['location', 'name']
        verbose_name_plural = 'Branches'

    def __str__(self):
        return f"{self.name} ({self.location.name})"
