"""
Layer 2 Models: Module C (Terms / Acceptance) & Module D (Programs / Packages / Catalog)

Module C:
- TermsDocument: Stable legal/policy document identity.
- TermsDocumentVersion: Immutable/effective-dated legal document versions.
- TermsAcceptance: Immutable proof of exact terms version acceptance.

Module D:
- ProgramCategory: Configurable program grouping.
- Program: Business program under which packages/classes are organized.
- Package: Stable package/plan identity.
- PackageVersion: Immutable published commercial version of a package.
- PackagePrice: Effective-dated price for an exact package version, optionally branch-specific.
- PackageBranchAvailability: Controls where a package may be sold/used.
- PackageEntitlementDefinition: Entitlement definitions frozen to an exact package version.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone


# ============================================================================
# MODULE C: TERMS / ACCEPTANCE
# ============================================================================

class TermsDocument(models.Model):
    """
    Stable legal/policy document identity.
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='terms_documents'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    document_type = models.CharField(
        max_length=100,
        help_text="MEMBERSHIP_TERMS, ATTENDANCE_COMMITMENT_POLICY, CANCELLATION_POLICY, PRIVACY_NOTICE, REWARD_POLICY, etc."
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'terms_documents'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_termsdoc_org_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class TermsDocumentVersion(models.Model):
    """
    Immutable/effective-dated legal document versions.
    Published ACTIVE/RETIRED versions are immutable.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terms_document = models.ForeignKey(
        TermsDocument, on_delete=models.PROTECT, related_name='versions'
    )
    version_number = models.PositiveIntegerField()
    content_text = models.TextField(null=True, blank=True)
    file = models.ForeignKey(
        'File', on_delete=models.PROTECT, null=True, blank=True, related_name='terms_versions'
    )
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    created_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.PROTECT, related_name='created_terms_versions'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'terms_document_versions'
        unique_together = [('terms_document', 'version_number')]
        indexes = [
            models.Index(fields=['terms_document', 'status', 'effective_from'], name='idx_termsver_doc_st_eff'),
        ]

    def __str__(self):
        return f"{self.terms_document.code} v{self.version_number} ({self.status})"


class TermsAcceptance(models.Model):
    """
    Immutable proof of exact terms version acceptance.
    No updates/deletes after acceptance except legally controlled anonymization.
    """
    CHANNEL_CHOICES = [
        ('WEB', 'Web'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk'),
        ('API', 'API'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terms_document_version = models.ForeignKey(
        TermsDocumentVersion, on_delete=models.PROTECT, related_name='acceptances'
    )
    lead = models.ForeignKey(
        'Lead', on_delete=models.PROTECT, null=True, blank=True, related_name='terms_acceptances'
    )
    user_profile = models.ForeignKey(
        'UserProfile', on_delete=models.PROTECT, null=True, blank=True, related_name='terms_acceptances'
    )
    order_id = models.UUIDField(null=True, blank=True, help_text="Related order ID if accepted at checkout")
    accepted_at = models.DateTimeField(default=timezone.now)
    accepted_via = models.CharField(max_length=30, choices=CHANNEL_CHOICES)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_metadata = models.JSONField(default=dict, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'terms_acceptances'
        indexes = [
            models.Index(fields=['user_profile', 'accepted_at'], name='idx_termsacc_user_dt'),
            models.Index(fields=['lead', 'accepted_at'], name='idx_termsacc_lead_dt'),
            models.Index(fields=['terms_document_version'], name='idx_termsacc_ver'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(lead__isnull=False) | models.Q(user_profile__isnull=False),
                name='chk_terms_acceptance_subject'
            )
        ]

    def __str__(self):
        subject = f"User {self.user_profile_id}" if self.user_profile_id else f"Lead {self.lead_id}"
        return f"{subject} accepted {self.terms_document_version} at {self.accepted_at}"


# ============================================================================
# MODULE D: PROGRAM / PACKAGE / CATALOG
# ============================================================================

class ProgramCategory(models.Model):
    """
    Configurable program grouping.
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='program_categories'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    description = models.TextField(null=True, blank=True)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'program_categories'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status', 'display_order'], name='idx_progcat_org_st_ord'),
        ]

    def __str__(self):
        return self.name


class Program(models.Model):
    """
    Business program under which packages/classes may be organized.
    """
    PROGRAM_TYPE_CHOICES = [
        ('MEMBERSHIP', 'Membership'),
        ('FITNESS', 'Fitness'),
        ('PERSONAL_TRAINING', 'Personal Training'),
        ('PILATES', 'Pilates'),
        ('ONLINE', 'Online'),
        ('HYBRID', 'Hybrid'),
        ('OTHER', 'Other'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='programs'
    )
    category = models.ForeignKey(
        ProgramCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='programs'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    program_type = models.CharField(max_length=40, choices=PROGRAM_TYPE_CHOICES, default='MEMBERSHIP')
    trial_allowed = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'programs'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_prog_org_status'),
            models.Index(fields=['category', 'status'], name='idx_prog_cat_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Package(models.Model):
    """
    Stable package/plan identity; commercial terms live in immutable PackageVersions.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='packages'
    )
    program = models.ForeignKey(
        Program, on_delete=models.PROTECT, null=True, blank=True, related_name='packages'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'packages'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_pkg_org_status'),
            models.Index(fields=['program', 'status'], name='idx_pkg_prog_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class PackageVersion(models.Model):
    """
    Immutable published commercial version of a package.
    Non-negotiable rule: Once published (FUTURE/ACTIVE/RETIRED), commercial fields cannot be mutated.
    """
    DURATION_UNIT_CHOICES = [
        ('DAY', 'Day(s)'),
        ('WEEK', 'Week(s)'),
        ('MONTH', 'Month(s)'),
        ('YEAR', 'Year(s)'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('FUTURE', 'Future'),
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        Package, on_delete=models.PROTECT, related_name='versions'
    )
    version_number = models.PositiveIntegerField()
    name_snapshot = models.CharField(max_length=200)
    description_snapshot = models.TextField(null=True, blank=True)
    duration_value = models.PositiveIntegerField()
    duration_unit = models.CharField(max_length=20, choices=DURATION_UNIT_CHOICES)
    validity_days = models.PositiveIntegerField(null=True, blank=True)
    is_trial_package = models.BooleanField(default=False)
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    created_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.PROTECT, related_name='created_package_versions'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_versions'
        unique_together = [('package', 'version_number')]
        indexes = [
            models.Index(fields=['package', 'status', 'effective_from'], name='idx_pkgver_pkg_st_eff'),
        ]

    def __str__(self):
        return f"{self.package.code} v{self.version_number} ({self.status})"


class PackagePrice(models.Model):
    """
    Effective-dated price for an exact package version, optionally branch-specific.
    """
    STATUS_CHOICES = [
        ('FUTURE', 'Future'),
        ('ACTIVE', 'Active'),
        ('EXPIRED', 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package_version = models.ForeignKey(
        PackageVersion, on_delete=models.PROTECT, related_name='prices'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='package_prices'
    )
    currency = models.CharField(max_length=3, default='INR')
    base_price = models.DecimalField(max_digits=14, decimal_places=2)
    tax_percent = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.000'))
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.PROTECT, related_name='created_package_prices'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_prices'
        indexes = [
            models.Index(fields=['package_version', 'branch', 'status', 'effective_from'], name='idx_pkgprc_ver_br_st_eff'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_price__gte=0),
                name='chk_package_price_base_price_non_neg'
            )
        ]

    @property
    def total_price(self) -> Decimal:
        tax_multiplier = Decimal('1.0') + (self.tax_percent / Decimal('100.0'))
        return (self.base_price * tax_multiplier).quantize(Decimal('0.01'))

    def __str__(self):
        branch_str = f" @ {self.branch.name}" if self.branch else " (All Branches)"
        return f"{self.package_version} - {self.currency} {self.base_price}{branch_str}"


class PackageBranchAvailability(models.Model):
    """
    Controls where a package may be sold/used as configured.
    """
    STATUS_CHOICES = [
        ('ENABLED', 'Enabled'),
        ('DISABLED', 'Disabled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        Package, on_delete=models.PROTECT, related_name='branch_availabilities'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='package_availabilities'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ENABLED')
    available_from = models.DateTimeField(null=True, blank=True)
    available_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_branch_availability'
        unique_together = [('package', 'branch')]
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_pkgbr_avail_br_status'),
            models.Index(fields=['package', 'status'], name='idx_pkgbr_avail_pkg_status'),
        ]

    def __str__(self):
        return f"{self.package.code} @ {self.branch.name} ({self.status})"


class PackageEntitlementDefinition(models.Model):
    """
    Entitlement definitions frozen to an exact package version.
    Immutable once package version is published.
    """
    ENTITLEMENT_TYPE_CHOICES = [
        ('HOME_BRANCH_SESSION', 'Home Branch Session'),
        ('CROSS_BRANCH_SESSION', 'Cross Branch Session'),
        ('CLASS_SESSION', 'Class Session'),
        ('PERSONAL_TRAINING_SESSION', 'Personal Training Session'),
        ('ASSESSMENT', 'Assessment'),
        ('OPEN_ACCESS', 'Open Access / Gym Floor'),
        ('OTHER', 'Other'),
    ]
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package_version = models.ForeignKey(
        PackageVersion, on_delete=models.PROTECT, related_name='entitlement_definitions'
    )
    entitlement_type = models.CharField(max_length=50, choices=ENTITLEMENT_TYPE_CHOICES)
    allocated_units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_unlimited = models.BooleanField(default=False)
    validity_days = models.PositiveIntegerField(null=True, blank=True)
    reference_type = models.CharField(max_length=100, null=True, blank=True, help_text="Scoped entity type e.g. ClassTemplate")
    reference_id = models.UUIDField(null=True, blank=True, help_text="Scoped entity ID")
    configuration = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_entitlement_definitions'
        indexes = [
            models.Index(fields=['package_version', 'status'], name='idx_pkgent_ver_status'),
            models.Index(fields=['entitlement_type'], name='idx_pkgent_type'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_unlimited=True) | models.Q(allocated_units__isnull=False),
                name='chk_pkg_entitlement_units_or_unlimited'
            )
        ]

    def __str__(self):
        units = "Unlimited" if self.is_unlimited else f"{self.allocated_units} units"
        return f"{self.package_version} - {self.entitlement_type}: {units}"
