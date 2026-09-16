"""
DRF Serializers for Layer 2: Module E (Group Classes, Scheduling, Content Studio & Demand Planning)
"""

from rest_framework import serializers
from .models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule, ClassSpecialtyRequirement,
    ClassScheduleImportBatch, ClassScheduleImportRow,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation,
)


class ClassCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassCategory
        fields = ['id', 'organization', 'code', 'name', 'description', 'display_order', 'status', 'created_at', 'updated_at']
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class ClassSpecialtyRequirementSerializer(serializers.ModelSerializer):
    specialty_name = serializers.CharField(source='trainer_specialty.name', read_only=True)
    specialty_code = serializers.CharField(source='trainer_specialty.code', read_only=True)

    class Meta:
        model = ClassSpecialtyRequirement
        fields = [
            'id', 'class_template', 'trainer_specialty', 'specialty_name',
            'specialty_code', 'minimum_proficiency_level', 'is_mandatory', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassPriceSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = ClassPrice
        fields = [
            'id', 'class_template', 'branch', 'branch_name', 'version_number',
            'currency', 'price', 'tax_percent', 'effective_from', 'effective_until',
            'status', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClassBranchAvailabilitySerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = ClassBranchAvailability
        fields = [
            'id', 'class_template', 'branch', 'branch_name', 'status',
            'capacity_override', 'trial_capacity_override', 'waitlist_capacity_override',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClassTemplateSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    program_name = serializers.CharField(source='program.name', read_only=True)
    specialty_requirements = ClassSpecialtyRequirementSerializer(many=True, read_only=True)

    class Meta:
        model = ClassTemplate
        fields = [
            'id', 'organization', 'category', 'category_name', 'program', 'program_name',
            'code', 'name', 'description', 'default_duration_minutes', 'default_capacity',
            'default_trial_capacity', 'default_waitlist_capacity', 'default_delivery_mode',
            'allow_booking', 'allow_trial', 'allow_waitlist', 'allow_reschedule',
            'min_age', 'max_age', 'status', 'specialty_requirements', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class ClassScheduleRuleSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_template.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = ClassScheduleRule
        fields = [
            'id', 'class_template', 'class_name', 'branch', 'branch_name',
            'recurrence_type', 'days_of_week', 'start_time', 'end_time',
            'valid_from', 'valid_until', 'delivery_mode', 'capacity_override',
            'trial_capacity_override', 'waitlist_capacity_override', 'status',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClassOccurrenceTrainerSerializer(serializers.ModelSerializer):
    trainer_name = serializers.CharField(source='trainer_profile.employee_profile.user_profile.user.full_name', read_only=True)
    trainer_code = serializers.CharField(source='trainer_profile.trainer_code', read_only=True)

    class Meta:
        model = ClassOccurrenceTrainer
        fields = [
            'id', 'occurrence', 'trainer_profile', 'trainer_code', 'trainer_name',
            'trainer_role', 'status', 'assigned_at', 'created_at'
        ]
        read_only_fields = ['id', 'assigned_at', 'created_at']


class ClassOccurrenceSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_template.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    trainers = ClassOccurrenceTrainerSerializer(source='trainer_assignments', many=True, read_only=True)

    class Meta:
        model = ClassOccurrence
        fields = [
            'id', 'class_template', 'class_name', 'schedule_rule', 'branch', 'branch_name',
            'occurrence_date', 'start_at', 'end_at', 'delivery_mode',
            'online_provider', 'online_join_url', 'capacity', 'trial_capacity',
            'waitlist_capacity', 'booking_open_at', 'booking_close_at',
            'cancellation_cutoff_at', 'status', 'is_manual', 'is_override',
            'trainers', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PackageClassAccessRuleSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_template.name', read_only=True)
    category_name = serializers.CharField(source='class_category.name', read_only=True)

    class Meta:
        model = PackageClassAccessRule
        fields = [
            'id', 'package_version', 'class_template', 'class_name',
            'class_category', 'category_name', 'branch', 'access_type',
            'entitlement_type', 'units_per_booking', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassScheduleImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassScheduleImportBatch
        fields = [
            'id', 'organization', 'branch', 'file', 'import_source', 'status',
            'total_rows', 'valid_rows', 'invalid_rows', 'imported_rows',
            'uploaded_at', 'completed_at', 'created_at'
        ]
        read_only_fields = ['id', 'uploaded_at', 'completed_at', 'created_at']


class ClassScheduleImportRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassScheduleImportRow
        fields = [
            'id', 'import_batch', 'row_number', 'class_name_input', 'class_template',
            'branch', 'trainer_name_input', 'trainer_profile', 'start_time', 'end_time',
            'days_of_week', 'valid_from', 'valid_until', 'delivery_mode',
            'validation_status', 'validation_errors', 'created_schedule_rule', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassContentItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassContentItem
        fields = [
            'id', 'organization', 'title', 'description', 'content_type',
            'external_url', 'file', 'display_order', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'organization', 'created_at']


class ClassContentMappingSerializer(serializers.ModelSerializer):
    content_title = serializers.CharField(source='content_item.title', read_only=True)

    class Meta:
        model = ClassContentMapping
        fields = [
            'id', 'content_item', 'content_title', 'class_template',
            'class_category', 'program', 'trainer_specialty', 'branch',
            'delivery_mode', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassContentAssignmentSerializer(serializers.ModelSerializer):
    content_title = serializers.CharField(source='content_item.title', read_only=True)

    class Meta:
        model = ClassContentAssignment
        fields = [
            'id', 'occurrence', 'content_item', 'content_title', 'trainer_profile',
            'rotation_cycle_number', 'rotation_position', 'assignment_method',
            'status', 'viewed_at', 'assigned_at', 'created_at'
        ]
        read_only_fields = ['id', 'assigned_at', 'created_at']


class ClassDemandEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassDemandEvent
        fields = [
            'id', 'class_template', 'occurrence', 'branch', 'user_profile',
            'booking_id', 'event_type', 'event_at', 'metadata', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassScheduleRecommendationSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_template.name', read_only=True)

    class Meta:
        model = ClassScheduleRecommendation
        fields = [
            'id', 'planning_run', 'branch', 'class_template', 'class_name',
            'recommended_day_of_week', 'recommended_date', 'recommended_start_time',
            'recommended_end_time', 'recommended_delivery_mode', 'recommended_capacity',
            'demand_score', 'recommendation_type', 'reason_codes', 'status',
            'manager_comment', 'reviewed_at', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class ClassDemandPlanningRunSerializer(serializers.ModelSerializer):
    recommendations = ClassScheduleRecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = ClassDemandPlanningRun
        fields = [
            'id', 'organization', 'branch', 'planning_type', 'analysis_from',
            'analysis_to', 'planning_from', 'planning_to', 'status',
            'generated_by_type', 'recommendation_count', 'recommendations',
            'approved_at', 'created_at'
        ]
        read_only_fields = ['id', 'organization', 'created_at']
