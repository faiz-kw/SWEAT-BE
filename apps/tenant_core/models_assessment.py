"""
apps/tenant_core/models_assessment.py — Assessment & Photorealistic Transformation Engine.

Models:
1. MemberAssessment: The end-to-end assessment session record holding client intake,
   baseline photos, CV posture & body composition metrics, dynamic system package match,
   milestone visual transformation images, and the manual Admin approval gate.
2. AssessmentMilestone: Discrete monthly progressive milestones (Month 1, 2, 3)
   with physiological progression data.
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
    Assessment session record.
    Access is gated by active ASSESSMENT entitlement or package opt-in.
    """
    GOAL_CHOICES = [
        ('FAT_LOSS', 'Fat Loss & Leanness'),
        ('POSTURE_ALIGNMENT', 'Posture & Core Alignment'),
        ('HYPERTROPHY_GLUTES', 'Lower Body & Glutes Toning'),
        ('STRENGTH_CONDITIONING', 'Strength & Conditioning'),
        ('ATHLETIC_PERFORMANCE', 'Athletic Performance & Stamina'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft / Intake In Progress'),
        ('PHOTOS_CAPTURED', 'Photos Captured'),
        ('ANALYZING', 'Analyzing Metrics'),
        ('PENDING_REVIEW', 'Pending Coach / Admin Approval'),
        ('APPROVED', 'Approved by Coach'),
        ('DISPATCHED', 'Report Dispatched to Member'),
        ('REJECTED', 'Reshoot / Reassessment Requested'),
    ]

    DISPATCH_STATUS_CHOICES = [
        ('NOT_REQUESTED', 'Not Requested'),
        ('PENDING_APPROVAL', 'Pending Admin Approval'),
        ('SENT', 'Sent via Gupshup WhatsApp'),
        ('FAILED', 'Dispatch Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='member_assessments')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='member_assessments')

    # Subject (Member or Lead)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='assessments')
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, null=True, blank=True, related_name='assessments')
    member_name = models.CharField(max_length=150, blank=True, default='')
    member_phone = models.CharField(max_length=30, blank=True, default='')

    # Operator (Trainer / Staff holding tablet)
    conducted_by_trainer = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='conducted_assessments'
    )

    # Entitlement Gating: Link to active Membership or Entitlement consumed
    membership = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments')
    assessment_entitlement = models.ForeignKey(
        MembershipEntitlement, on_delete=models.SET_NULL, null=True, blank=True, related_name='assessments'
    )

    # 1. User Interactive Intake (Asked directly to member)
    target_goal = models.CharField(max_length=40, choices=GOAL_CHOICES, default='FAT_LOSS')
    target_timeline_months = models.PositiveIntegerField(default=6, help_text="User commitment timeline (e.g. 3, 6, 12 months)")
    weekly_frequency_days = models.PositiveIntegerField(default=3, help_text="Planned workout days per week (e.g. 3, 4, 5 days)")
    user_notes = models.TextField(null=True, blank=True, help_text="Specific focus areas or injuries mentioned by the user")

    # 2. Baseline Photos
    photo_front = models.ImageField(upload_to='assessments/baseline/%Y/%m/', null=True, blank=True)
    photo_side = models.ImageField(upload_to='assessments/baseline/%Y/%m/', null=True, blank=True)
    photo_front_url = models.CharField(max_length=500, null=True, blank=True)
    photo_side_url = models.CharField(max_length=500, null=True, blank=True)

    # 3. Auto-Calculated Computer Vision Metrics
    posture_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('85.00'), help_text="0-100 posture score")
    shoulder_tilt_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    hip_tilt_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    forward_head_angle_degrees = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    estimated_body_fat_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    waist_to_hip_ratio = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    measured_height_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    measured_weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    cv_analysis_payload = models.JSONField(default=dict, blank=True, help_text="Raw CV landmarks and posture coordinates")

    # 4. Dynamic System Program Recommendation (Matched directly to DB Packages)
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

    # 5. Photorealistic Transformation Visual Milestones (Month 1, 2, 3)
    month_1_image_url = models.CharField(max_length=500, null=True, blank=True)
    month_2_image_url = models.CharField(max_length=500, null=True, blank=True)
    month_3_image_url = models.CharField(max_length=500, null=True, blank=True)
    milestone_metadata = models.JSONField(default=dict, blank=True)

    # 6. Session Lifecycle & Manual Admin Approval Gate
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='DRAFT', db_index=True)
    reviewed_by_user = models.ForeignKey(
        TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_assessments'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    coach_notes = models.TextField(null=True, blank=True)
    workout_prescription = models.TextField(null=True, blank=True)
    diet_prescription = models.TextField(null=True, blank=True)

    # 7. WhatsApp Delivery Status (Held until Admin Clicks "Approve & Send")
    whatsapp_dispatch_status = models.CharField(max_length=30, choices=DISPATCH_STATUS_CHOICES, default='PENDING_APPROVAL')
    whatsapp_message_id = models.CharField(max_length=150, null=True, blank=True)
    whatsapp_dispatched_at = models.DateTimeField(null=True, blank=True)

    # 8. Studio Screen Pairing Passcode
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
        return f"Assessment for {name} ({self.get_status_display()})"