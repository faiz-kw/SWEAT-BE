"""
apps/tenant_core/models_assessment.py — Functional Fitness & Movement Assessment Engine.

Purpose:
Establish the member's current functional fitness, movement capability, cardiovascular endurance,
mobility, balance and strength, then personalize training, nutrition targets, and track progress.

17-Section Comprehensive Framework:
- Section 1: Member Profile & Training Baseline
- Section 2: Health Readiness & Safety Screening (PAR-Q)
- Section 3: Baseline Body & Vital Measurements + RMR/TDEE
- Sections 4-11: Standardized Movement Battery (Lower, Upper, Core, Balance, Mobility, Cardio)
- Sections 12-14: Member Goals, Nutrition Targets & Capability Matrix
- Sections 15-17: Reassessment, Consents & Audit
"""

import uuid
import secrets
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import UserProfile, Lead
from .models_catalog import Program, Package, PackageVersion
from .models_memberships import Membership, MembershipEntitlement


def _generate_session_code():
    """Generates a friendly 6-digit numeric pairing code for studio screen casting."""
    return str(secrets.randbelow(900000) + 100000)


class MemberAssessment(models.Model):
    """
    Standardized Functional Fitness & Movement Assessment record.
    """
    ASSESSMENT_TYPE_CHOICES = [
        ('INITIAL', 'Initial Baseline Assessment'),
        ('MONTHLY', 'Monthly Progress Evaluation'),
        ('QUARTERLY', 'Quarterly Milestone Review'),
        ('POST_PROGRAM', 'Post-Program Outcome Assessment'),
        ('RETURN_TO_TRAINING', 'Return After Break / Rehabilitation'),
    ]

    TRAINING_EXPERIENCE_CHOICES = [
        ('BEGINNER', 'Beginner (New to consistent exercise)'),
        ('RETURNING', 'Returning after a break / injury'),
        ('INTERMEDIATE', 'Intermediate (1-2 years regular training)'),
        ('ADVANCED', 'Advanced / Athletic'),
    ]

    ACTIVITY_LEVEL_CHOICES = [
        ('SEDENTARY', 'Mostly Sedentary (Desk work, minimal exercise)'),
        ('LIGHTLY_ACTIVE', 'Lightly Active (1-2 days/week light exercise)'),
        ('MODERATELY_ACTIVE', 'Moderately Active (3-4 days/week training)'),
        ('HIGHLY_ACTIVE', 'Highly Active (5+ days/week intense exercise/sports)'),
    ]

    TRAINING_STYLE_CHOICES = [
        ('PILATES', 'Reformer Pilates (Core & Spine focus)'),
        ('BOOTCAMP', 'HIIT Bootcamp & Conditioning'),
        ('STRENGTH', 'Strength & Hypertrophy'),
        ('FUNCTIONAL', 'Functional Fitness & Longevity'),
        ('MIXED', 'Mixed Studio Training'),
    ]

    GOAL_CHOICES = [
        ('FAT_LOSS', 'Fat Loss & Leanness'),
        ('POSTURE_ALIGNMENT', 'Posture & Spine Alignment'),
        ('HYPERTROPHY_GLUTES', 'Lower Body & Glutes Toning'),
        ('STRENGTH_CONDITIONING', 'Strength & Conditioning'),
        ('ATHLETIC_PERFORMANCE', 'Athletic Performance & Stamina'),
        ('MOBILITY_LONGEVITY', 'Mobility, Flexibility & Healthy Ageing'),
        ('GENERAL_FITNESS', 'General Health & Wellbeing'),
    ]

    SCREENING_OUTCOME_CHOICES = [
        ('CLEARED', 'Cleared to proceed with non-clinical assessment'),
        ('PROCEED_WITH_MODIFICATIONS', 'Proceed only with selected modifications'),
        ('DEFER_MEDICAL_CLEARANCE', 'Defer pending medical / physician guidance'),
        ('STOPPED_SAFETY_CONCERNS', 'Stopped due to symptoms or safety concerns'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft / Intake In Progress'),
        ('TESTING', 'Physical Testing In Progress'),
        ('PENDING_REVIEW', 'Pending Coach / Admin Approval'),
        ('APPROVED', 'Approved by Coach (Unlocked for Studio Screen Display)'),
        ('REJECTED', 'Reshoot / Reassessment Requested'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True, related_name='member_assessments')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='member_assessments')

    # Subject (Member or Lead)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='assessments')
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, null=True, blank=True, related_name='assessments')
    member_name = models.CharField(max_length=150, blank=True, default='')
    member_phone = models.CharField(max_length=30, blank=True, default='')

    # Assessor (Coach conducting the session)
    conducted_by_trainer = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='conducted_assessments'
    )

    # Entitlement Gating (Optional)
    membership = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments')
    assessment_entitlement = models.ForeignKey(
        MembershipEntitlement, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments'
    )

    # =========================================================================
    # SECTION 1: MEMBER PROFILE & BASELINE CLASSIFICATION
    # =========================================================================
    assessment_type = models.CharField(max_length=30, choices=ASSESSMENT_TYPE_CHOICES, default='INITIAL')
    training_experience = models.CharField(max_length=30, choices=TRAINING_EXPERIENCE_CHOICES, default='BEGINNER')
    current_activity_level = models.CharField(max_length=30, choices=ACTIVITY_LEVEL_CHOICES, default='MODERATELY_ACTIVE')
    preferred_training_style = models.CharField(max_length=30, choices=TRAINING_STYLE_CHOICES, default='PILATES')

    # Goals & Outcomes
    target_goal = models.CharField(max_length=40, choices=GOAL_CHOICES, default='POSTURE_ALIGNMENT')
    stated_goal_outcome = models.TextField(null=True, blank=True, help_text="Member's own words of what they want to achieve")
    target_timeline_months = models.PositiveIntegerField(default=3, help_text="Committed duration in months")
    target_timeline_weeks = models.PositiveIntegerField(default=12, help_text="Committed duration in weeks")
    weekly_frequency_days = models.PositiveIntegerField(default=3, help_text="Planned workout days per week")
    perceived_barrier = models.CharField(max_length=150, null=True, blank=True, help_text="Time, confidence, previous injury, etc.")
    member_confidence_score = models.PositiveIntegerField(default=8, help_text="1 to 10 scale")
    user_notes = models.TextField(null=True, blank=True, help_text="Additional intake notes")

    # =========================================================================
    # SECTION 2: READINESS, HEALTH & SAFETY SCREENING (PAR-Q)
    # =========================================================================
    health_screening = models.JSONField(
        default=dict,
        blank=True,
        help_text="Responses to 10 readiness questions: feeling_well, chest_pain, fainting, breathlessness, pain, injury, surgery, etc."
    )
    screening_outcome = models.CharField(max_length=35, choices=SCREENING_OUTCOME_CHOICES, default='CLEARED')
    screening_coach_notes = models.TextField(null=True, blank=True, help_text="Restrictions, modifications, or physician follow-up")

    # =========================================================================
    # SECTION 3: BASELINE BODY, VITALS & METABOLIC ESTIMATES
    # =========================================================================
    measured_height_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    measured_weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    waist_circumference_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    resting_heart_rate_bpm = models.PositiveIntegerField(null=True, blank=True)
    resting_blood_pressure = models.CharField(max_length=20, null=True, blank=True, help_text="e.g. 120/80")

    # Derived Body Composition
    calculated_bmi = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    estimated_body_fat_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    skeletal_muscle_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    waist_to_hip_ratio = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)

    # Metabolic & Nutrition Targets
    bmr_rmr_kcal = models.PositiveIntegerField(null=True, blank=True, help_text="Resting Metabolic Rate in kcal/day (Mifflin-St Jeor)")
    tdee_kcal = models.PositiveIntegerField(null=True, blank=True, help_text="Total Daily Energy Expenditure in kcal/day")
    daily_calorie_target = models.PositiveIntegerField(null=True, blank=True, help_text="Calorie target with goal deficit/surplus")
    nutrition_macros = models.JSONField(
        default=dict,
        blank=True,
        help_text="Personalized macros: protein_g, carbs_g, fat_g, fiber_g, hydration_liters, guidelines"
    )
    measurement_metadata = models.JSONField(default=dict, blank=True, help_text="Source per metric: measured/calculated/estimated")

    # =========================================================================
    # SECTIONS 4–11: FUNCTIONAL MOVEMENT & PERFORMANCE BATTERY (FAST-LOGGED)
    # =========================================================================
    functional_tests_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Results across Lower Body, Upper Body, Core, Balance, Mobility, and Cardio test batteries"
    )

    # Capability Matrix Summary (Section 13)
    capability_matrix = models.JSONField(
        default=dict,
        blank=True,
        help_text="Domain ratings: lower_body, upper_body, core, balance, mobility, cardio (Beginner/Developing/Established)"
    )

    # =========================================================================
    # CAMERA POSTURE & COMPUTER VISION
    # =========================================================================
    photo_front = models.ImageField(upload_to='assessments/baseline/%Y/%m/', null=True, blank=True)
    photo_side = models.ImageField(upload_to='assessments/baseline/%Y/%m/', null=True, blank=True)
    photo_front_url = models.CharField(max_length=500, null=True, blank=True)
    photo_side_url = models.CharField(max_length=500, null=True, blank=True)

    posture_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('85.00'), help_text="0-100 posture score")
    shoulder_tilt_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    hip_tilt_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    forward_head_angle_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    cv_analysis_payload = models.JSONField(default=dict, blank=True, help_text="Pose landmarks and symmetry vectors")

    # =========================================================================
    # REVEALED AFTER APPROVAL: PROGRAM, PACKAGE & NUTRITION PLAN
    # =========================================================================
    recommended_program = models.ForeignKey(
        Program, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments_recommended'
    )
    recommended_package = models.ForeignKey(
        Package, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments_recommended'
    )
    recommended_package_version = models.ForeignKey(
        PackageVersion, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments_recommended'
    )
    recommendation_reason = models.TextField(null=True, blank=True)
    alternative_packages_payload = models.JSONField(default=list, blank=True)

    # Transformation Milestones (Shown on Studio TV display only after approval)
    month_1_image_url = models.CharField(max_length=500, null=True, blank=True)
    month_2_image_url = models.CharField(max_length=500, null=True, blank=True)
    month_3_image_url = models.CharField(max_length=500, null=True, blank=True)
    milestone_metadata = models.JSONField(default=dict, blank=True)

    # =========================================================================
    # COACH REVIEW & UNLOCK GATE
    # =========================================================================
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    reviewed_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_assessments'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    coach_strengths = models.TextField(null=True, blank=True)
    coach_areas_to_develop = models.TextField(null=True, blank=True)
    workout_prescription = models.TextField(null=True, blank=True)
    diet_prescription = models.TextField(null=True, blank=True)
    whatsapp_dispatch_status = models.CharField(max_length=30, default='NOT_REQUESTED')
    reassessment_interval_weeks = models.PositiveIntegerField(default=6)

    # Consents (Section 16)
    consent_body_measurements = models.BooleanField(default=True)
    consent_movement_photos = models.BooleanField(default=True)
    consent_ai_analysis = models.BooleanField(default=True)

    # Studio Screen Pairing
    pairing_code = models.CharField(max_length=10, default=_generate_session_code)
    display_screen_connected = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'member_assessments'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status'], name='idx_mbreval_org_st'),
            models.Index(fields=['user_profile', 'status'], name='idx_mbreval_usr_st'),
            models.Index(fields=['pairing_code'], name='idx_mbreval_pair_code'),
        ]

    def __str__(self):
        name = self.member_name or (self.user_profile.full_name if self.user_profile else 'Guest')
        return f"Functional Assessment for {name} ({self.get_status_display()})"
