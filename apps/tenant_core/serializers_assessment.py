"""
serializers_assessment.py — DRF Serializers for Functional Assessment Module.

Replaces legacy manual dict building in views_assessment.py with validated, strictly typed serializers.
Includes:
- HealthScreeningSerializer (10 PAR-Q questions)
- VitalsMeasurementSerializer (biometrics + provenance tags)
- FunctionalTestItemSerializer (protocols, variations, symptoms)
- AssessmentTestProgressionSerializer (Section 14 exercise progressions)
- ProgressionApprovalSerializer (Coach sign-off input)
- MemberAssessmentDetailSerializer (Comprehensive detail view)
- MemberAssessmentWriteSerializer (Intake & update operations)
- AssessmentApprovalSerializer (Coach approval payload)
"""

from decimal import Decimal
from rest_framework import serializers
from .models_assessment import MemberAssessment, AssessmentTestProgression
from .models_org import Branch
from .models_crm import UserProfile
from .models_users import TenantUser
from .services_media import build_signed_media_url
from .security_utils import mask_phone_if_restricted


class HealthScreeningSerializer(serializers.Serializer):
    heart_condition = serializers.BooleanField(default=False)
    chest_pain_activity = serializers.BooleanField(default=False)
    chest_pain_rest = serializers.BooleanField(default=False)
    dizziness_loss_of_balance = serializers.BooleanField(default=False)
    bone_joint_problem = serializers.BooleanField(default=False)
    blood_pressure_medication = serializers.BooleanField(default=False)
    unexplained_shortness_of_breath = serializers.BooleanField(default=False)
    recent_surgery_injury = serializers.BooleanField(default=False)
    pregnancy_or_postpartum = serializers.BooleanField(default=False)
    other_medical_condition = serializers.BooleanField(default=False)
    details = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False, default=dict)


class VitalsMeasurementSerializer(serializers.Serializer):
    height_cm = serializers.FloatField(required=False, allow_null=True)
    weight_kg = serializers.FloatField(required=False, allow_null=True)
    waist_cm = serializers.FloatField(required=False, allow_null=True)
    resting_hr = serializers.IntegerField(required=False, allow_null=True)
    resting_bp = serializers.CharField(required=False, allow_blank=True, default='')
    bmi = serializers.FloatField(required=False, allow_null=True)
    rmr_kcal = serializers.IntegerField(required=False, allow_null=True)
    tdee_kcal = serializers.IntegerField(required=False, allow_null=True)
    daily_calorie_target = serializers.IntegerField(required=False, allow_null=True)
    macros = serializers.DictField(required=False, default=dict)
    provenance = serializers.DictField(
        child=serializers.ChoiceField(
            choices=['MEASURED', 'CALCULATED', 'ESTIMATED', 'MEMBER_REPORTED', 'UNAVAILABLE']
        ),
        required=False,
        default=dict,
    )
    disclaimers = serializers.DictField(child=serializers.CharField(), required=False, default=dict)


class FunctionalTestItemSerializer(serializers.Serializer):
    protocol_version = serializers.CharField(default='v1.0')
    test_name = serializers.CharField(max_length=100)
    domain = serializers.ChoiceField(
        choices=['lower_body', 'upper_body', 'core', 'balance', 'mobility', 'cardiovascular']
    )
    variation = serializers.CharField(max_length=100, allow_blank=True, default='Standard')
    reps = serializers.IntegerField(required=False, allow_null=True)
    load_kg = serializers.FloatField(required=False, allow_null=True)
    hold_seconds = serializers.IntegerField(required=False, allow_null=True)
    movement_quality = serializers.CharField(max_length=50, allow_blank=True, default='Meets criteria')
    status = serializers.ChoiceField(
        choices=['VALID', 'MODIFIED', 'INCOMPLETE', 'NOT_ASSESSED'],
        default='VALID'
    )
    symptoms = serializers.CharField(allow_blank=True, default='')


class AssessmentTestProgressionSerializer(serializers.ModelSerializer):
    coach_approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AssessmentTestProgression
        fields = [
            'id',
            'assessment',
            'user_profile',
            'exercise_name',
            'current_variation',
            'current_load_kg',
            'current_assistance',
            'current_reps_or_duration',
            'movement_quality',
            'reported_symptoms',
            'next_progression_target',
            'progression_criteria',
            'review_date',
            'coach_approved_by',
            'coach_approved_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'coach_approved_by', 'created_at', 'updated_at']

    def get_coach_approved_by_name(self, obj) -> str:
        if obj.coach_approved_by:
            return f"{obj.coach_approved_by.first_name} {obj.coach_approved_by.last_name}".strip()
        return ''


class ProgressionApprovalSerializer(serializers.Serializer):
    criteria_met_notes = serializers.CharField(required=False, allow_blank=True, default='')
    next_variation = serializers.CharField(max_length=100, required=True)
    review_date = serializers.DateField(required=False, allow_null=True)


class MemberAssessmentDetailSerializer(serializers.ModelSerializer):
    branch_detail = serializers.SerializerMethodField()
    masked_phone = serializers.SerializerMethodField()
    vitals = serializers.SerializerMethodField()
    functional_tests = serializers.SerializerMethodField()
    capability_matrix = serializers.SerializerMethodField()
    symptoms_and_safety_events = serializers.SerializerMethodField()
    recommended_package = serializers.SerializerMethodField()
    alternatives = serializers.SerializerMethodField()
    signed_off_by_name = serializers.SerializerMethodField()
    photo_front_url = serializers.SerializerMethodField()
    photo_side_url = serializers.SerializerMethodField()

    class Meta:
        model = MemberAssessment
        fields = [
            'id',
            'pairing_code',
            'status',
            'assessment_type',
            'member_name',
            'member_phone',
            'masked_phone',
            'branch',
            'branch_detail',
            'previous_assessment',
            'training_experience',
            'current_activity_level',
            'preferred_training_style',
            'target_goal',
            'stated_goal_outcome',
            'target_timeline_months',
            'target_timeline_weeks',
            'weekly_frequency_days',
            'perceived_barrier',
            'member_confidence_score',
            'user_notes',
            # Health Screening
            'health_screening',
            'screening_outcome',
            'screening_coach_notes',
            # Vitals
            'vitals',
            # Movement Battery
            'functional_tests',
            'capability_matrix',
            'symptoms_and_safety_events',
            # Catalog Recommendations
            'recommended_package',
            'alternatives',
            # Consents
            'consent_body_measurements',
            'consent_movement_photos',
            'consent_ai_analysis',
            'consent_secondary_use',
            'consent_notice_version',
            # Governance & Sign-Offs
            'member_acknowledged_at',
            'coach_signed_off_at',
            'coach_signed_off_by',
            'signed_off_by_name',
            'reassessment_due_date',
            # Coach Prescriptions
            'coach_strengths',
            'coach_areas_to_develop',
            'workout_prescription',
            'diet_prescription',
            'reassessment_interval_weeks',
            # Media
            'photo_front_url',
            'photo_side_url',
            # Timestamps
            'created_at',
            'updated_at',
            'reviewed_at',
        ]

    def get_branch_detail(self, obj) -> dict:
        if obj.branch:
            return {'id': str(obj.branch.id), 'name': obj.branch.name}
        return None

    def get_masked_phone(self, obj) -> str:
        req = self.context.get('request')
        return mask_phone_if_restricted(req, obj.member_phone)

    def get_vitals(self, obj) -> dict:
        return {
            'height_cm': float(obj.measured_height_cm) if obj.measured_height_cm else None,
            'weight_kg': float(obj.measured_weight_kg) if obj.measured_weight_kg else None,
            'waist_cm': float(obj.waist_circumference_cm) if obj.waist_circumference_cm else None,
            'resting_hr': obj.resting_heart_rate_bpm,
            'resting_bp': obj.resting_blood_pressure,
            'bmi': float(obj.calculated_bmi) if obj.calculated_bmi else None,
            'rmr_kcal': obj.bmr_rmr_kcal,
            'tdee_kcal': obj.tdee_kcal,
            'daily_calorie_target': obj.daily_calorie_target,
            'macros': obj.nutrition_macros or {},
            'disclaimers': {
                'bmi': 'BMI is a screening metric, not a direct measure of body fat or muscularity.',
                'rmr': 'Estimated via Mifflin-St Jeor formula with physical activity multiplier.',
            },
        }

    def get_functional_tests(self, obj) -> dict:
        return obj.functional_tests_payload or {}

    def get_capability_matrix(self, obj) -> dict:
        return obj.capability_matrix or {}

    def get_symptoms_and_safety_events(self, obj) -> dict:
        return obj.symptoms_and_safety_events or {}

    def get_recommended_package(self, obj) -> dict:
        rec_pkg = obj.recommended_package
        if not rec_pkg:
            return None
        rec_price = None
        rec_sessions = None
        ver = obj.recommended_package_version or rec_pkg.get_active_version()
        if ver:
            p = ver.prices.filter(status='ACTIVE').first()
            if p:
                rec_price = float(p.total_price)
            for e in ver.entitlement_definitions.filter(status='ACTIVE'):
                if e.entitlement_type in ['HOME_BRANCH_SESSION', 'CLASS_SESSION', 'PERSONAL_TRAINING_SESSION', 'OPEN_ACCESS']:
                    rec_sessions = 365 if e.is_unlimited else int(e.allocated_units or 0)

        return {
            'id': str(rec_pkg.id),
            'name': rec_pkg.name,
            'session_units': rec_sessions or 72,
            'price': rec_price or 0,
            'reason': obj.recommendation_reason,
        }

    def get_alternatives(self, obj) -> list:
        return obj.alternative_packages_payload or []

    def get_signed_off_by_name(self, obj) -> str:
        if obj.coach_signed_off_by:
            return f"{obj.coach_signed_off_by.first_name} {obj.coach_signed_off_by.last_name}".strip()
        return ''

    def get_photo_front_url(self, obj) -> str:
        if obj.photo_front:
            return build_signed_media_url(obj.photo_front.name)
        return obj.photo_front_url or ''

    def get_photo_side_url(self, obj) -> str:
        if obj.photo_side:
            return build_signed_media_url(obj.photo_side.name)
        return obj.photo_side_url or ''


class MemberAssessmentWriteSerializer(serializers.ModelSerializer):
    user_profile_id = serializers.UUIDField(required=False, allow_null=True)
    branch_id = serializers.UUIDField(required=False, allow_null=True)
    previous_assessment_id = serializers.UUIDField(required=False, allow_null=True)
    package_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = MemberAssessment
        fields = [
            'user_profile_id',
            'branch_id',
            'previous_assessment_id',
            'package_id',
            'assessment_type',
            'member_name',
            'member_phone',
            'training_experience',
            'current_activity_level',
            'preferred_training_style',
            'target_goal',
            'stated_goal_outcome',
            'target_timeline_months',
            'target_timeline_weeks',
            'weekly_frequency_days',
            'perceived_barrier',
            'member_confidence_score',
            'user_notes',
            'health_screening',
            'screening_outcome',
            'screening_coach_notes',
            'measured_height_cm',
            'measured_weight_kg',
            'waist_circumference_cm',
            'resting_heart_rate_bpm',
            'resting_blood_pressure',
            'functional_tests_payload',
            'symptoms_and_safety_events',
            'consent_body_measurements',
            'consent_movement_photos',
            'consent_ai_analysis',
            'consent_secondary_use',
            'consent_notice_version',
            'coach_strengths',
            'coach_areas_to_develop',
            'workout_prescription',
            'diet_prescription',
        ]


class AssessmentApprovalSerializer(serializers.Serializer):
    coach_notes = serializers.CharField(required=False, allow_blank=True, default='')
    workout_prescription = serializers.CharField(required=False, allow_blank=True, default='')
    diet_prescription = serializers.CharField(required=False, allow_blank=True, default='')
    reassessment_interval_weeks = serializers.IntegerField(required=False, default=6)
    send_whatsapp = serializers.BooleanField(required=False, default=False)
