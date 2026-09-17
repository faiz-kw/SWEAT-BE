"""
Layer 2 Models: Module E (Group Classes / Scheduling / Content / Demand)

17 models:
1. ClassCategory: Configurable group-class category catalog.
2. ClassTemplate: Stable definition of a group class.
3. ClassPrice: Versioned/effective-dated pay-per-use price for a class.
4. ClassBranchAvailability: Class availability and branch-specific capacity overrides.
5. ClassScheduleRule: Recurring schedule definition used to generate class occurrences.
6. ClassOccurrence: Actual dated group-class slot that receives trainer assignments and bookings.
7. ClassOccurrenceTrainer: Trainer assignment to an actual group-class occurrence.
8. PackageClassAccessRule: Defines class inclusion/exclusion for an exact package version.
9. ClassSpecialtyRequirement: Optional trainer specialty requirements for a group class.
10. ClassScheduleImportBatch: Audit/control header for bulk class schedule imports.
11. ClassScheduleImportRow: Staging/validation row for bulk class scheduling.
12. ClassContentItem: Content Studio library item used as trainer reference content.
13. ClassContentMapping: Defines which class/content pool an item is eligible for.
14. ClassContentAssignment: Occurrence-level content selection and rotation history.
15. ClassDemandEvent: Append-only measurable demand/behavior event stream.
16. ClassDemandPlanningRun: Explainable demand-analysis run producing a future class plan.
17. ClassScheduleRecommendation: Explainable proposed class slot from demand planning.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone


# ============================================================================
# 1. CATALOG & TEMPLATES
# ============================================================================

class ClassCategory(models.Model):
    """
    Configurable group-class category catalog.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='class_categories'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    description = models.TextField(null=True, blank=True)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_categories'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status', 'display_order'], name='idx_clscat_org_st_ord'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class ClassTemplate(models.Model):
    """
    Stable definition of a group class; may be standalone or linked to a program.
    """
    DELIVERY_MODE_CHOICES = [
        ('OFFLINE', 'Offline / Studio'),
        ('ONLINE', 'Online Live Stream'),
        ('HYBRID', 'Hybrid'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('ARCHIVED', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='class_templates'
    )
    category = models.ForeignKey(
        ClassCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='class_templates'
    )
    program = models.ForeignKey(
        'Program', on_delete=models.PROTECT, null=True, blank=True, related_name='class_templates'
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    default_duration_minutes = models.PositiveIntegerField(default=60)
    default_capacity = models.PositiveIntegerField(default=20)
    default_trial_capacity = models.PositiveIntegerField(default=0)
    default_waitlist_capacity = models.PositiveIntegerField(default=0)
    default_delivery_mode = models.CharField(max_length=20, choices=DELIVERY_MODE_CHOICES, default='OFFLINE')
    allow_booking = models.BooleanField(default=True)
    allow_trial = models.BooleanField(default=False)
    allow_waitlist = models.BooleanField(default=False)
    allow_reschedule = models.BooleanField(default=True)
    min_age = models.PositiveIntegerField(null=True, blank=True)
    max_age = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_templates'
        unique_together = [('organization', 'code')]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_clstpl_org_status'),
            models.Index(fields=['program', 'status'], name='idx_clstpl_prog_status'),
            models.Index(fields=['category', 'status'], name='idx_clstpl_cat_status'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class ClassPrice(models.Model):
    """
    Versioned/effective-dated pay-per-use price for a class.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='prices'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='class_prices'
    )
    version_number = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default='INR')
    price = models.DecimalField(max_digits=14, decimal_places=2)
    tax_percent = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('0.000'))
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.PROTECT, related_name='created_class_prices'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_prices'
        unique_together = [('class_template', 'branch', 'version_number')]
        indexes = [
            models.Index(fields=['class_template', 'branch', 'status', 'effective_from'], name='idx_clsprc_tpl_br_st_eff'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name='chk_class_price_non_neg')
        ]

    def __str__(self):
        return f"{self.class_template.code} v{self.version_number} - {self.currency} {self.price}"


class ClassBranchAvailability(models.Model):
    """
    Class availability and branch-specific capacity overrides.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='branch_availabilities'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='class_availabilities'
    )
    status = models.CharField(max_length=20, default='ENABLED')
    capacity_override = models.PositiveIntegerField(null=True, blank=True)
    trial_capacity_override = models.PositiveIntegerField(null=True, blank=True)
    waitlist_capacity_override = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_branch_availability'
        unique_together = [('class_template', 'branch')]
        indexes = [
            models.Index(fields=['branch', 'status'], name='idx_clsbr_avail_br_status'),
            models.Index(fields=['class_template', 'status'], name='idx_clsbr_avail_tpl_status'),
        ]

    def __str__(self):
        return f"{self.class_template.code} @ {self.branch.name} ({self.status})"


# ============================================================================
# 2. SCHEDULING & OCCURRENCES
# ============================================================================

class ClassScheduleRule(models.Model):
    """
    Recurring schedule definition used to generate class occurrences.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='schedule_rules'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='class_schedule_rules'
    )
    recurrence_type = models.CharField(max_length=20, default='WEEKLY')
    days_of_week = models.JSONField(default=list, blank=True, null=True, help_text="[1, 2, ..., 7] for Mon-Sun")
    start_time = models.TimeField()
    end_time = models.TimeField()
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    delivery_mode = models.CharField(max_length=20, default='OFFLINE')
    recurrence_config = models.JSONField(default=dict, blank=True, null=True)
    capacity_override = models.PositiveIntegerField(null=True, blank=True)
    trial_capacity_override = models.PositiveIntegerField(null=True, blank=True)
    waitlist_capacity_override = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_schedule_rules'
        indexes = [
            models.Index(fields=['branch', 'status', 'valid_from'], name='idx_clssch_br_st_vf'),
            models.Index(fields=['class_template', 'status'], name='idx_clssch_tpl_status'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(end_time__gt=models.F('start_time')), name='chk_schedule_rule_end_gt_start'),
        ]

    def __str__(self):
        return f"{self.class_template.name} @ {self.branch.name} ({self.start_time}-{self.end_time})"


class ClassOccurrence(models.Model):
    """
    Actual dated group-class slot that receives trainer assignments and bookings.
    """
    STATUS_CHOICES = [
        ('SCHEDULED', 'Scheduled'),
        ('OPEN', 'Open for Booking'),
        ('FULL', 'Capacity Reached / Full'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='occurrences'
    )
    schedule_rule = models.ForeignKey(
        ClassScheduleRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_occurrences'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='class_occurrences'
    )
    occurrence_date = models.DateField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    delivery_mode = models.CharField(max_length=20, default='OFFLINE')
    online_provider = models.CharField(max_length=100, null=True, blank=True)
    online_join_url = models.TextField(null=True, blank=True)
    capacity = models.PositiveIntegerField(default=20)
    trial_capacity = models.PositiveIntegerField(default=0)
    waitlist_capacity = models.PositiveIntegerField(default=0)
    booking_open_at = models.DateTimeField(null=True, blank=True)
    booking_close_at = models.DateTimeField(null=True, blank=True)
    cancellation_cutoff_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='SCHEDULED')
    is_manual = models.BooleanField(default=False)
    is_override = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_occurrences'
        indexes = [
            models.Index(fields=['branch', 'start_at', 'status'], name='idx_clsocc_br_st_status'),
            models.Index(fields=['class_template', 'start_at'], name='idx_clsocc_tpl_start'),
            models.Index(fields=['schedule_rule'], name='idx_clsocc_rule'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(end_at__gt=models.F('start_at')), name='chk_occurrence_end_gt_start'),
        ]

    def save(self, *args, **kwargs):
        if not self.occurrence_date and self.start_at:
            self.occurrence_date = self.start_at.date()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.class_template.name} @ {self.branch.name} on {self.occurrence_date} ({self.status})"


class ClassOccurrenceTrainer(models.Model):
    """
    Trainer assignment to an actual group-class occurrence.
    Must enforce specialty eligibility, shifts, buffer, and conflicts.
    """
    ROLE_CHOICES = [
        ('LEAD', 'Lead Trainer'),
        ('ASSISTANT', 'Assistant Trainer'),
        ('SUBSTITUTE', 'Substitute Trainer'),
    ]
    STATUS_CHOICES = [
        ('ASSIGNED', 'Assigned'),
        ('CONFIRMED', 'Confirmed'),
        ('CANCELLED', 'Cancelled'),
        ('REPLACED', 'Replaced'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(
        ClassOccurrence, on_delete=models.PROTECT, related_name='trainer_assignments'
    )
    trainer_profile = models.ForeignKey(
        'TrainerProfile', on_delete=models.PROTECT, related_name='class_assignments'
    )
    trainer_role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='LEAD')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ASSIGNED')
    assigned_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_class_trainers'
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_occurrence_trainers'
        indexes = [
            models.Index(fields=['occurrence', 'status'], name='idx_clstrn_occ_status'),
            models.Index(fields=['trainer_profile', 'status'], name='idx_clstrn_prof_status'),
        ]

    def __str__(self):
        return f"{self.trainer_profile} -> {self.occurrence} ({self.trainer_role})"


# ============================================================================
# 3. ACCESS RULES & SPECIALTY REQUIREMENTS
# ============================================================================

class PackageClassAccessRule(models.Model):
    """
    Defines class inclusion/exclusion for an exact package version.
    """
    ACCESS_TYPE_CHOICES = [
        ('INCLUDED', 'Included in Package'),
        ('EXCLUDED', 'Explicitly Excluded'),
        ('DISCOUNTED', 'Discounted Rate'),
        ('ADD_ON', 'Add-On'),
        ('PAY_PER_USE', 'Pay Per Use'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package_version = models.ForeignKey(
        'PackageVersion', on_delete=models.PROTECT, related_name='class_access_rules'
    )
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='package_access_rules'
    )
    class_category = models.ForeignKey(
        ClassCategory, on_delete=models.PROTECT, null=True, blank=True, related_name='package_access_rules'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='package_class_rules'
    )
    access_type = models.CharField(max_length=20, choices=ACCESS_TYPE_CHOICES, default='INCLUDED')
    entitlement_type = models.CharField(max_length=50, null=True, blank=True)
    units_per_booking = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1.00'))
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_class_access_rules'
        indexes = [
            models.Index(fields=['package_version', 'status'], name='idx_pkgcls_ver_status'),
            models.Index(fields=['class_template'], name='idx_pkgcls_tpl'),
            models.Index(fields=['class_category'], name='idx_pkgcls_cat'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(class_template__isnull=False) | models.Q(class_category__isnull=False),
                name='chk_pkg_class_rule_target'
            )
        ]

    def __str__(self):
        target = self.class_template.name if self.class_template else self.class_category.name
        return f"{self.package_version} -> {target} ({self.access_type})"


class ClassSpecialtyRequirement(models.Model):
    """
    Optional trainer specialty requirements for a group class.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='specialty_requirements'
    )
    trainer_specialty = models.ForeignKey(
        'TrainerSpecialty', on_delete=models.PROTECT, related_name='class_requirements'
    )
    minimum_proficiency_level = models.CharField(max_length=20, null=True, blank=True)
    is_mandatory = models.BooleanField(default=True)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_specialty_requirements'
        unique_together = [('class_template', 'trainer_specialty')]
        indexes = [
            models.Index(fields=['class_template', 'status'], name='idx_clsspec_tpl_status'),
            models.Index(fields=['trainer_specialty', 'status'], name='idx_clsspec_spec_status'),
        ]

    def __str__(self):
        return f"{self.class_template.name} requires {self.trainer_specialty.name}"


# ============================================================================
# 4. BULK SCHEDULE IMPORT
# ============================================================================

class ClassScheduleImportBatch(models.Model):
    """
    Audit/control header for bulk class schedule imports.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='class_import_batches'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='class_import_batches'
    )
    file = models.ForeignKey(
        'File', on_delete=models.PROTECT, null=True, blank=True, related_name='class_import_batches'
    )
    import_source = models.CharField(max_length=20, default='CSV')
    status = models.CharField(max_length=30, default='PENDING')
    total_rows = models.IntegerField(default=0)
    valid_rows = models.IntegerField(default=0)
    invalid_rows = models.IntegerField(default=0)
    imported_rows = models.IntegerField(default=0)
    uploaded_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.PROTECT, related_name='uploaded_class_imports'
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_schedule_import_batches'
        indexes = [
            models.Index(fields=['organization', 'status', 'uploaded_at'], name='idx_clsimp_org_st_up'),
            models.Index(fields=['branch', 'uploaded_at'], name='idx_clsimp_br_up'),
        ]

    def __str__(self):
        return f"Import Batch {self.id} ({self.status}) - {self.imported_rows}/{self.total_rows} rows"


class ClassScheduleImportRow(models.Model):
    """
    Staging/validation row for bulk class scheduling.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    import_batch = models.ForeignKey(
        ClassScheduleImportBatch, on_delete=models.CASCADE, related_name='rows'
    )
    row_number = models.IntegerField()
    class_name_input = models.CharField(max_length=250)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.SET_NULL, null=True, blank=True
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.SET_NULL, null=True, blank=True
    )
    trainer_name_input = models.CharField(max_length=250, null=True, blank=True)
    trainer_profile = models.ForeignKey(
        'TrainerProfile', on_delete=models.SET_NULL, null=True, blank=True
    )
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    days_of_week = models.JSONField(default=list, blank=True, null=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    delivery_mode = models.CharField(max_length=20, null=True, blank=True)
    validation_status = models.CharField(max_length=20, default='PENDING')
    validation_errors = models.JSONField(default=list, blank=True, null=True)
    created_schedule_rule = models.ForeignKey(
        ClassScheduleRule, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_schedule_import_rows'
        unique_together = [('import_batch', 'row_number')]
        indexes = [
            models.Index(fields=['import_batch', 'validation_status'], name='idx_clsrow_batch_status'),
        ]

    def __str__(self):
        return f"Row {self.row_number}: {self.class_name_input} ({self.validation_status})"


# ============================================================================
# 5. CONTENT STUDIO
# ============================================================================

class ClassContentItem(models.Model):
    """
    Content Studio library item used as trainer reference content for class occurrences.
    """
    CONTENT_TYPE_CHOICES = [
        ('VIDEO', 'Video Workout / Flow'),
        ('PDF_GUIDE', 'PDF Workout Card / Guide'),
        ('AUDIO', 'Audio Playlist / Track'),
        ('ARTICLE', 'Instruction Article'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='class_content_items'
    )
    title = models.CharField(max_length=250)
    description = models.TextField(null=True, blank=True)
    content_type = models.CharField(max_length=30, choices=CONTENT_TYPE_CHOICES, default='VIDEO')
    external_url = models.TextField(null=True, blank=True)
    file = models.ForeignKey(
        'File', on_delete=models.PROTECT, null=True, blank=True, related_name='content_items'
    )
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_content_items'
        indexes = [
            models.Index(fields=['organization', 'status', 'display_order'], name='idx_clsitm_org_st_ord'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(external_url__isnull=False) | models.Q(file__isnull=False),
                name='chk_content_item_has_source'
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.content_type})"


class ClassContentMapping(models.Model):
    """
    Defines which class/content pool an item is eligible for.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_item = models.ForeignKey(
        ClassContentItem, on_delete=models.PROTECT, related_name='mappings'
    )
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, null=True, blank=True, related_name='content_mappings'
    )
    class_category = models.ForeignKey(
        ClassCategory, on_delete=models.PROTECT, null=True, blank=True, related_name='content_mappings'
    )
    program = models.ForeignKey(
        'Program', on_delete=models.PROTECT, null=True, blank=True, related_name='content_mappings'
    )
    trainer_specialty = models.ForeignKey(
        'TrainerSpecialty', on_delete=models.PROTECT, null=True, blank=True, related_name='content_mappings'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='content_mappings'
    )
    delivery_mode = models.CharField(max_length=20, null=True, blank=True)
    status = models.CharField(max_length=20, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_content_mappings'
        indexes = [
            models.Index(fields=['content_item', 'status'], name='idx_clsmap_item_status'),
            models.Index(fields=['class_template', 'status'], name='idx_clsmap_tpl_status'),
            models.Index(fields=['program', 'status'], name='idx_clsmap_prog_status'),
            models.Index(fields=['trainer_specialty', 'status'], name='idx_clsmap_spec_status'),
        ]

    def __str__(self):
        return f"{self.content_item.title} mapped pool ({self.status})"


class ClassContentAssignment(models.Model):
    """
    Occurrence-level content selection and rotation history.
    Non-negotiable rule: NO_REPEAT_UNTIL_EXHAUSTED rotation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurrence = models.ForeignKey(
        ClassOccurrence, on_delete=models.PROTECT, related_name='content_assignments'
    )
    content_item = models.ForeignKey(
        ClassContentItem, on_delete=models.PROTECT, related_name='occurrence_assignments'
    )
    trainer_profile = models.ForeignKey(
        'TrainerProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_class_content'
    )
    rotation_cycle_number = models.IntegerField(default=1)
    rotation_position = models.IntegerField(default=1)
    assignment_method = models.CharField(max_length=20, default='ROTATION')
    assigned_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_content_records'
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, default='ACTIVE')
    viewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'class_content_assignments'
        indexes = [
            models.Index(fields=['occurrence', 'status'], name='idx_clsass_occ_status'),
            models.Index(fields=['content_item', 'rotation_cycle_number'], name='idx_clsass_item_cycle'),
            models.Index(fields=['trainer_profile', 'assigned_at'], name='idx_clsass_trn_dt'),
        ]

    def __str__(self):
        return f"{self.occurrence} -> {self.content_item.title} (Cycle {self.rotation_cycle_number}, Pos {self.rotation_position})"


# ============================================================================
# 6. DEMAND PLANNING
# ============================================================================

class ClassDemandEvent(models.Model):
    """
    Append-only measurable demand/behavior event stream for group classes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='demand_events'
    )
    occurrence = models.ForeignKey(
        ClassOccurrence, on_delete=models.SET_NULL, null=True, blank=True, related_name='demand_events'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='class_demand_events'
    )
    user_profile = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='class_demand_events'
    )
    booking_id = models.UUIDField(null=True, blank=True)
    event_type = models.CharField(
        max_length=40,
        help_text="BOOKING_CREATED, WAITLIST_JOINED, ATTENDED, NO_SHOW, CANCELLED_LATE, CANCELLED_EARLY"
    )
    event_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'class_demand_events'
        indexes = [
            models.Index(fields=['branch', 'class_template', 'event_at'], name='idx_dmd_br_tpl_dt'),
            models.Index(fields=['occurrence', 'event_at'], name='idx_dmd_occ_dt'),
            models.Index(fields=['event_type', 'event_at'], name='idx_dmd_type_dt'),
        ]

    def __str__(self):
        return f"{self.event_type} for {self.class_template.name} @ {self.branch.name}"


class ClassDemandPlanningRun(models.Model):
    """
    One explainable demand-analysis run producing a future class plan and file.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'Organization', on_delete=models.PROTECT, related_name='demand_planning_runs'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='demand_planning_runs'
    )
    planning_type = models.CharField(max_length=20, default='WEEKLY')
    analysis_from = models.DateField()
    analysis_to = models.DateField()
    planning_from = models.DateField()
    planning_to = models.DateField()
    status = models.CharField(max_length=30, default='PROPOSED')
    generated_by_type = models.CharField(max_length=20, default='ALGORITHM')
    generated_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='generated_planning_runs'
    )
    approved_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_planning_runs'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    recommendation_count = models.IntegerField(default=0)
    generated_file = models.ForeignKey(
        'File', on_delete=models.SET_NULL, null=True, blank=True, related_name='demand_planning_runs'
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_demand_planning_runs'
        indexes = [
            models.Index(fields=['organization', 'status', 'planning_from'], name='idx_dmdrun_org_st_pf'),
            models.Index(fields=['branch', 'planning_from'], name='idx_dmdrun_br_pf'),
        ]

    def __str__(self):
        return f"Demand Run {self.id} ({self.status}) - {self.planning_from} to {self.planning_to}"


class ClassScheduleRecommendation(models.Model):
    """
    Explainable proposed class slot/action from a demand planning run.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    planning_run = models.ForeignKey(
        ClassDemandPlanningRun, on_delete=models.CASCADE, related_name='recommendations'
    )
    branch = models.ForeignKey(
        'Branch', on_delete=models.PROTECT, related_name='schedule_recommendations'
    )
    class_template = models.ForeignKey(
        ClassTemplate, on_delete=models.PROTECT, related_name='schedule_recommendations'
    )
    recommended_day_of_week = models.SmallIntegerField(null=True, blank=True)
    recommended_date = models.DateField(null=True, blank=True)
    recommended_start_time = models.TimeField()
    recommended_end_time = models.TimeField()
    recommended_delivery_mode = models.CharField(max_length=20, default='OFFLINE')
    recommended_capacity = models.IntegerField(default=20)
    recommended_occurrence_count = models.IntegerField(default=1)
    recommended_trainer_specialty = models.ForeignKey(
        'TrainerSpecialty', on_delete=models.SET_NULL, null=True, blank=True
    )
    recommended_trainer_profile = models.ForeignKey(
        'TrainerProfile', on_delete=models.SET_NULL, null=True, blank=True
    )
    recommended_content_item = models.ForeignKey(
        ClassContentItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    demand_score = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    recommendation_type = models.CharField(max_length=30, default='ADD_OCCURRENCE')
    reason_codes = models.JSONField(default=list, blank=True, null=True)
    historical_metrics = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(max_length=20, default='PROPOSED')
    manager_comment = models.TextField(null=True, blank=True)
    reviewed_by_user = models.ForeignKey(
        'TenantUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_recommendations'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_schedule_recommendations'
        indexes = [
            models.Index(fields=['planning_run', 'status'], name='idx_dmdrec_run_status'),
            models.Index(fields=['branch', 'class_template', 'recommended_date'], name='idx_dmdrec_br_tpl_dt'),
        ]

    def __str__(self):
        return f"{self.class_template.name} slot recommendation ({self.status}) - Score: {self.demand_score}"
