"""
apps/tenant_core/models_crm.py — Layer 2 Module B: CRM, Leads, Intake Forms, Trials & Sales

15 Domain Models:
  1. LeadSource (lead_sources)
  2. Lead (leads)
  3. LeadStatusHistory (lead_status_history)
  4. LeadAssignment (lead_assignments)
  5. LeadNote (lead_notes)
  6. LeadActivity (lead_activities)
  7. IntakeForm (intake_forms)
  8. IntakeQuestion (intake_questions)
  9. IntakeQuestionOption (intake_question_options)
  10. IntakeSubmission (intake_submissions)
  11. IntakeAnswer (intake_answers)
  12. TrialBooking (trial_bookings)
  13. TrialStatusHistory (trial_status_history)
  14. LeadConversion (lead_conversions)
  15. SalesFollowupTask (sales_followup_tasks)

Operates strictly in the dedicated tenant database (app_label='tenant_core').
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile, TrainerProfile


class LeadSource(models.Model):
    """
    Catalog of acquisition channels and attribution sources.
    """
    SOURCE_TYPES = [
        ('WALK_IN', 'Walk In'),
        ('WEBSITE', 'Website'),
        ('MOBILE_APP', 'Mobile App'),
        ('META', 'Meta / Facebook / Instagram'),
        ('GOOGLE', 'Google Search / Ads'),
        ('WHATSAPP', 'WhatsApp'),
        ('PHONE', 'Phone / Direct Call'),
        ('REFERRAL', 'Member / Friend Referral'),
        ('TRAINER', 'Trainer Outreach'),
        ('EMPLOYEE', 'Staff Outreach'),
        ('SALES', 'Sales Representative'),
        ('CAMPAIGN', 'Marketing Campaign'),
        ('OTHER', 'Other'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='lead_sources',
        db_column='organization_id',
    )
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    source_type = models.CharField(max_length=30, choices=SOURCE_TYPES)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_sources'
        constraints = [
            models.UniqueConstraint(fields=['organization', 'code'], name='uq_lead_source_org_code'),
        ]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_lead_src_org_status'),
        ]

    def __str__(self):
        return f"LeadSource({self.code} - {self.name})"


class Lead(models.Model):
    """
    Prospective member record progressing through the commercial lifecycle.
    """
    STATUSES = [
        ('NEW_LEAD', 'New Lead'),
        ('TRIAL_BOOKED', 'Trial Booked'),
        ('TRIAL_CONFIRMED', 'Trial Confirmed'),
        ('TRIAL_ATTENDED', 'Trial Attended'),
        ('NO_SHOW', 'No Show'),
        ('FOLLOW_UP_PENDING', 'Follow-up Pending'),
        ('INTERESTED', 'Interested'),
        ('HOT_LEAD', 'Hot Lead'),
        ('PAYMENT_PENDING', 'Payment Pending'),
        ('CONVERTED', 'Converted'),
        ('NOT_INTERESTED', 'Not Interested'),
        ('LOST', 'Lost'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='leads',
        db_column='organization_id',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leads',
        db_column='branch_id',
    )
    lead_source = models.ForeignKey(
        LeadSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leads',
        db_column='lead_source_id',
    )
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    phone_normalized = models.CharField(max_length=32, null=True, blank=True)
    email_normalized = models.CharField(max_length=254, null=True, blank=True)
    gender = models.CharField(max_length=30, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    occupation = models.CharField(max_length=150, null=True, blank=True)
    company_name = models.CharField(max_length=200, null=True, blank=True)
    area = models.CharField(max_length=200, null=True, blank=True)
    current_status = models.CharField(max_length=40, choices=STATUSES, default='NEW_LEAD')
    assigned_sales_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_sales_leads',
        db_column='assigned_sales_user_id',
    )
    assigned_trainer_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_trainer_leads',
        db_column='assigned_trainer_user_id',
    )
    first_touch_source = models.CharField(max_length=100, null=True, blank=True)
    latest_touch_source = models.CharField(max_length=100, null=True, blank=True)
    campaign_reference = models.CharField(max_length=200, null=True, blank=True)
    converted_user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='converted_from_leads',
        db_column='converted_user_profile_id',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'leads'
        indexes = [
            models.Index(fields=['organization', 'current_status'], name='idx_lead_org_status'),
            models.Index(fields=['branch', 'current_status'], name='idx_lead_branch_status'),
            models.Index(fields=['phone_normalized'], name='idx_lead_phone'),
            models.Index(fields=['email_normalized'], name='idx_lead_email'),
            models.Index(fields=['assigned_sales_user'], name='idx_lead_sales_user'),
        ]

    def __str__(self):
        return f"Lead({self.first_name} {self.last_name} - {self.current_status})"


class LeadStatusHistory(models.Model):
    """
    Append-only chronological audit log of lead status changes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='status_history',
        db_column='lead_id',
    )
    from_status = models.CharField(max_length=40, null=True, blank=True)
    to_status = models.CharField(max_length=40)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    changed_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='changed_by_user_id',
        related_name='lead_status_changes',
    )
    changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_status_history'
        indexes = [
            models.Index(fields=['lead', 'changed_at'], name='idx_lead_stat_hist_time'),
        ]

    def __str__(self):
        return f"StatusChange(Lead:{self.lead_id} {self.from_status}->{self.to_status})"


class LeadAssignment(models.Model):
    """
    History of sales, trainer, and manager assignments to a lead.
    """
    ASSIGNMENT_TYPES = [
        ('SALES', 'Sales Representative'),
        ('TRAINER', 'Coach / Trainer'),
        ('MANAGER', 'Branch / Studio Manager'),
        ('OTHER', 'Other'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='assignments',
        db_column='lead_id',
    )
    assigned_to_user = models.ForeignKey(
        TenantUser,
        on_delete=models.PROTECT,
        related_name='lead_assignments_received',
        db_column='assigned_to_user_id',
    )
    assigned_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lead_assignments_given',
        db_column='assigned_by_user_id',
    )
    assignment_type = models.CharField(max_length=20, choices=ASSIGNMENT_TYPES, default='SALES')
    assigned_at = models.DateTimeField(default=timezone.now)
    unassigned_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_assignments'
        indexes = [
            models.Index(fields=['lead', 'status'], name='idx_lead_assign_status'),
            models.Index(fields=['assigned_to_user', 'status'], name='idx_lead_assign_user'),
        ]

    def __str__(self):
        return f"Assignment(Lead:{self.lead_id} -> User:{self.assigned_to_user_id} [{self.assignment_type}])"


class LeadNote(models.Model):
    """
    Structured notes and CRM commentary regarding a lead.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='notes',
        db_column='lead_id',
    )
    note_text = models.TextField()
    note_type = models.CharField(max_length=50, null=True, blank=True, default='GENERAL')
    created_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.PROTECT,
        related_name='lead_notes',
        db_column='created_by_user_id',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_notes'
        indexes = [
            models.Index(fields=['lead', '-created_at'], name='idx_lead_notes_time'),
        ]

    def __str__(self):
        return f"Note(Lead:{self.lead_id} by {self.created_by_user_id})"


class LeadActivity(models.Model):
    """
    Logged interactions with a lead (calls, meetings, whatsapp, trials, visits).
    """
    ACTIVITY_TYPES = [
        ('CALL', 'Phone Call'),
        ('WHATSAPP', 'WhatsApp Message'),
        ('EMAIL', 'Email'),
        ('VISIT', 'Studio Visit'),
        ('MEETING', 'Consultation Meeting'),
        ('FOLLOW_UP', 'Follow-up Call'),
        ('TRIAL', 'Trial Session'),
        ('PAYMENT_LINK', 'Payment Link Sent'),
        ('OTHER', 'Other Interaction'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='activities',
        db_column='lead_id',
    )
    activity_type = models.CharField(max_length=30, choices=ACTIVITY_TYPES)
    outcome = models.CharField(max_length=150, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    performed_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lead_activities_performed',
        db_column='performed_by_user_id',
    )
    activity_at = models.DateTimeField(default=timezone.now)
    external_reference = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_activities'
        indexes = [
            models.Index(fields=['lead', '-activity_at'], name='idx_lead_act_time'),
            models.Index(fields=['activity_type'], name='idx_lead_act_type'),
        ]

    def __str__(self):
        return f"Activity({self.activity_type} on Lead:{self.lead_id} outcome={self.outcome})"


class IntakeForm(models.Model):
    """
    Versioned questionnaire definition (PAR-Q, lifestyle, fitness goals).
    """
    STATUSES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='intake_forms',
        db_column='organization_id',
    )
    name = models.CharField(max_length=200)
    form_type = models.CharField(max_length=100)
    version_number = models.IntegerField(default=1)
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='DRAFT')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'intake_forms'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'form_type', 'version_number'],
                name='uq_intake_form_ver',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_intake_form_status'),
        ]

    def __str__(self):
        return f"IntakeForm({self.name} v{self.version_number} [{self.status}])"


class IntakeQuestion(models.Model):
    """
    Configurable questions belonging to an intake form.
    is_sensitive: Flag indicating protected medical / PAR-Q health information.
    """
    QUESTION_TYPES = [
        ('TEXT', 'Short Text'),
        ('NUMBER', 'Number'),
        ('DATE', 'Date'),
        ('SINGLE_SELECT', 'Single Select'),
        ('MULTI_SELECT', 'Multi Select'),
        ('BOOLEAN', 'Yes / No'),
        ('SCALE', 'Rating Scale'),
    ]
    CATEGORIES = [
        ('FITNESS', 'Fitness Background'),
        ('LIFESTYLE', 'Lifestyle & Nutrition'),
        ('PSYCHOLOGY', 'Motivation & Mindset'),
        ('MEDICAL', 'Medical / Health (Protected)'),
        ('SALES', 'Commercial / Preferences'),
        ('OTHER', 'Other'),
    ]
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intake_form = models.ForeignKey(
        IntakeForm,
        on_delete=models.PROTECT,
        related_name='questions',
        db_column='intake_form_id',
    )
    question_text = models.TextField()
    question_type = models.CharField(max_length=30, choices=QUESTION_TYPES)
    category = models.CharField(max_length=30, choices=CATEGORIES, default='FITNESS')
    is_required = models.BooleanField(default=False)
    is_sensitive = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'intake_questions'
        ordering = ['display_order', 'created_at']
        indexes = [
            models.Index(fields=['intake_form', 'display_order'], name='idx_intake_q_order'),
            models.Index(fields=['is_sensitive'], name='idx_intake_q_sensitive'),
        ]

    def __str__(self):
        return f"Question({self.question_text[:40]}... [{self.category}])"


class IntakeQuestionOption(models.Model):
    """
    Selectable choices for SINGLE_SELECT and MULTI_SELECT questions.
    """
    STATUSES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(
        IntakeQuestion,
        on_delete=models.PROTECT,
        related_name='options',
        db_column='question_id',
    )
    label = models.CharField(max_length=250)
    value = models.CharField(max_length=250)
    display_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUSES, default='ACTIVE')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'intake_question_options'
        ordering = ['display_order']

    def __str__(self):
        return f"Option({self.label} = {self.value})"


class IntakeSubmission(models.Model):
    """
    Instance of a completed questionnaire filled by a lead or member.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intake_form = models.ForeignKey(
        IntakeForm,
        on_delete=models.PROTECT,
        related_name='submissions',
        db_column='intake_form_id',
    )
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='intake_submissions',
        db_column='lead_id',
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='intake_submissions',
        db_column='user_profile_id',
    )
    submitted_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_intakes',
        db_column='submitted_by_user_id',
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'intake_submissions'
        indexes = [
            models.Index(fields=['lead', '-submitted_at'], name='idx_intake_sub_lead'),
            models.Index(fields=['user_profile', '-submitted_at'], name='idx_intake_sub_uprof'),
        ]

    def __str__(self):
        return f"Submission(Form:{self.intake_form_id} Lead:{self.lead_id} at {self.submitted_at})"


class IntakeAnswer(models.Model):
    """
    Individual response value for a question within a submission.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        IntakeSubmission,
        on_delete=models.PROTECT,
        related_name='answers',
        db_column='submission_id',
    )
    question = models.ForeignKey(
        IntakeQuestion,
        on_delete=models.PROTECT,
        related_name='answers',
        db_column='question_id',
    )
    text_value = models.TextField(null=True, blank=True)
    numeric_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    boolean_value = models.BooleanField(null=True, blank=True)
    date_value = models.DateField(null=True, blank=True)
    json_value = models.JSONField(default=dict, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'intake_answers'
        constraints = [
            models.UniqueConstraint(fields=['submission', 'question'], name='uq_sub_question_ans'),
        ]

    def __str__(self):
        return f"Answer(Sub:{self.submission_id} Q:{self.question_id})"


class TrialBooking(models.Model):
    """
    Free or discounted introductory session booking for prospective members.
    """
    STATUSES = [
        ('BOOKED', 'Booked'),
        ('CONFIRMED', 'Confirmed'),
        ('ATTENDED', 'Attended'),
        ('NO_SHOW', 'No Show'),
        ('CANCELLED', 'Cancelled'),
        ('RESCHEDULED', 'Rescheduled'),
        ('CONVERTED', 'Converted to Member'),
    ]
    BOOKING_SOURCES = [
        ('WEB', 'Website'),
        ('MOBILE_APP', 'Mobile App'),
        ('FRONT_DESK', 'Front Desk'),
        ('SALES', 'Sales Rep'),
        ('ADMIN', 'Admin'),
        ('OTHER', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='trial_bookings',
        db_column='lead_id',
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='trial_bookings',
        db_column='user_profile_id',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name='trial_bookings',
        db_column='branch_id',
    )
    class_occurrence_id = models.UUIDField(
        null=True,
        blank=True,
        help_text='Logical FK to class_occurrences.id once Module E is active',
    )
    assigned_trainer_profile = models.ForeignKey(
        TrainerProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='trial_bookings',
        db_column='assigned_trainer_profile_id',
    )
    trial_type = models.CharField(max_length=100, null=True, blank=True, default='GROUP_CLASS')
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    status = models.CharField(max_length=30, choices=STATUSES, default='BOOKED')
    booking_source = models.CharField(max_length=30, choices=BOOKING_SOURCES, default='WEB')
    created_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_trials',
        db_column='created_by_user_id',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'trial_bookings'
        indexes = [
            models.Index(fields=['lead', 'status'], name='idx_trial_lead_status'),
            models.Index(fields=['branch', 'scheduled_start'], name='idx_trial_branch_start'),
            models.Index(fields=['status'], name='idx_trial_status'),
        ]

    def __str__(self):
        return f"TrialBooking(Lead:{self.lead_id} Start:{self.scheduled_start} [{self.status}])"


class TrialStatusHistory(models.Model):
    """
    Append-only tracking of trial transitions (BOOKED -> CONFIRMED -> ATTENDED).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trial_booking = models.ForeignKey(
        TrialBooking,
        on_delete=models.PROTECT,
        related_name='status_history',
        db_column='trial_booking_id',
    )
    from_status = models.CharField(max_length=30, null=True, blank=True)
    to_status = models.CharField(max_length=30)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    changed_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='changed_by_user_id',
        related_name='trial_status_changes',
    )
    changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'trial_status_history'
        indexes = [
            models.Index(fields=['trial_booking', 'changed_at'], name='idx_trial_stat_time'),
        ]

    def __str__(self):
        return f"TrialChange(Trial:{self.trial_booking_id} {self.from_status}->{self.to_status})"


class LeadConversion(models.Model):
    """
    Commercial transaction record marking the conversion of a lead into a paid member.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='conversions',
        db_column='lead_id',
    )
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        related_name='lead_conversions',
        db_column='user_profile_id',
    )
    converted_at = models.DateTimeField(default=timezone.now)
    converted_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='performed_conversions',
        db_column='converted_by_user_id',
    )
    order_id = models.UUIDField(null=True, blank=True, help_text='Logical FK to orders.id')
    membership_id = models.UUIDField(null=True, blank=True, help_text='Logical FK to memberships.id')
    package_id = models.UUIDField(null=True, blank=True, help_text='Logical FK to packages.id')
    package_version_id = models.UUIDField(null=True, blank=True, help_text='Logical FK to package_versions.id')
    conversion_source = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'lead_conversions'
        indexes = [
            models.Index(fields=['lead'], name='idx_conv_lead'),
            models.Index(fields=['user_profile'], name='idx_conv_uprof'),
            models.Index(fields=['converted_at'], name='idx_conv_time'),
        ]

    def __str__(self):
        return f"Conversion(Lead:{self.lead_id} -> Member:{self.user_profile_id} at {self.converted_at})"


class SalesFollowupTask(models.Model):
    """
    Scheduled sales workflow task (call, whatsapp, payment reminder) assigned to a rep.
    """
    TASK_TYPES = [
        ('CALL', 'Phone Call'),
        ('WHATSAPP', 'WhatsApp Message'),
        ('EMAIL', 'Email Outreach'),
        ('MEETING', 'Consultation Meeting'),
        ('PAYMENT', 'Payment Follow-up'),
        ('TRIAL_FOLLOWUP', 'Post-Trial Follow-up'),
        ('REJOIN', 'Rejoin / Winback Outreach'),
        ('OTHER', 'Other Task'),
    ]
    PRIORITIES = [
        ('LOW', 'Low'),
        ('NORMAL', 'Normal'),
        ('HIGH', 'High'),
        ('URGENT', 'Urgent'),
    ]
    STATUSES = [
        ('PENDING', 'Pending'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(
        Lead,
        on_delete=models.PROTECT,
        related_name='followup_tasks',
        db_column='lead_id',
    )
    assigned_to_user = models.ForeignKey(
        TenantUser,
        on_delete=models.PROTECT,
        related_name='assigned_followup_tasks',
        db_column='assigned_to_user_id',
    )
    task_type = models.CharField(max_length=30, choices=TASK_TYPES)
    priority = models.CharField(max_length=20, choices=PRIORITIES, default='NORMAL')
    due_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING')
    outcome = models.TextField(null=True, blank=True)
    next_followup_at = models.DateTimeField(null=True, blank=True)
    created_by_user = models.ForeignKey(
        TenantUser,
        on_delete=models.PROTECT,
        related_name='created_followup_tasks',
        db_column='created_by_user_id',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'sales_followup_tasks'
        indexes = [
            models.Index(fields=['assigned_to_user', 'status', 'due_at'], name='idx_sft_user_stat_due'),
            models.Index(fields=['lead', 'status'], name='idx_sft_lead_stat'),
        ]

    def __str__(self):
        return f"FollowupTask({self.task_type} for Lead:{self.lead_id} due {self.due_at} [{self.status}])"
