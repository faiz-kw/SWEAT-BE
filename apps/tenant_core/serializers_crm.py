"""
apps/tenant_core/serializers_crm.py — DRF Serializers for Layer 2 Module B: CRM, Leads, Trials & Sales
"""

from rest_framework import serializers
from .models_crm import (
    LeadSource,
    Lead,
    LeadStatusHistory,
    LeadAssignment,
    LeadNote,
    LeadActivity,
    IntakeForm,
    IntakeQuestion,
    IntakeQuestionOption,
    IntakeSubmission,
    IntakeAnswer,
    TrialBooking,
    TrialStatusHistory,
    LeadConversion,
    SalesFollowupTask,
)


class LeadSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadSource
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class LeadSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    source_name = serializers.CharField(source='lead_source.name', read_only=True)
    assigned_sales_name = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    def get_assigned_sales_name(self, obj):
        u = obj.assigned_sales_user
        if not u:
            return None
        return f"{u.first_name} {u.last_name}".strip() or u.email


class LeadStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadStatusHistory
        fields = '__all__'
        read_only_fields = ['id', 'created_at']

    def get_changed_by_name(self, obj):
        u = obj.changed_by_user
        if not u:
            return 'System'
        return f"{u.first_name} {u.last_name}".strip() or u.email


class LeadAssignmentSerializer(serializers.ModelSerializer):
    assigned_to_name = serializers.SerializerMethodField()
    assigned_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadAssignment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_assigned_to_name(self, obj):
        u = obj.assigned_to_user
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_assigned_by_name(self, obj):
        u = obj.assigned_by_user
        if not u:
            return 'System'
        return f"{u.first_name} {u.last_name}".strip() or u.email


class LeadNoteSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadNote
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        u = obj.created_by_user
        return f"{u.first_name} {u.last_name}".strip() or u.email


class LeadActivitySerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadActivity
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_performed_by_name(self, obj):
        u = obj.performed_by_user
        if not u:
            return 'System'
        return f"{u.first_name} {u.last_name}".strip() or u.email


class IntakeQuestionOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntakeQuestionOption
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class IntakeQuestionSerializer(serializers.ModelSerializer):
    options = IntakeQuestionOptionSerializer(many=True, read_only=True)

    class Meta:
        model = IntakeQuestion
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class IntakeFormSerializer(serializers.ModelSerializer):
    questions = IntakeQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = IntakeForm
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class IntakeAnswerSerializer(serializers.ModelSerializer):
    question_text = serializers.CharField(source='question.question_text', read_only=True)
    is_sensitive = serializers.BooleanField(source='question.is_sensitive', read_only=True)

    class Meta:
        model = IntakeAnswer
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class IntakeSubmissionSerializer(serializers.ModelSerializer):
    form_name = serializers.CharField(source='intake_form.name', read_only=True)
    answers = IntakeAnswerSerializer(many=True, read_only=True)

    class Meta:
        model = IntakeSubmission
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class TrialBookingSerializer(serializers.ModelSerializer):
    lead_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    trainer_name = serializers.SerializerMethodField()

    class Meta:
        model = TrialBooking
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_lead_name(self, obj):
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()

    def get_trainer_name(self, obj):
        tp = obj.assigned_trainer_profile
        if not tp:
            return None
        return tp.trainer_code


class TrialStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = TrialStatusHistory
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class LeadConversionSerializer(serializers.ModelSerializer):
    lead_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadConversion
        fields = '__all__'
        read_only_fields = ['id', 'created_at']

    def get_lead_name(self, obj):
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()


class SalesFollowupTaskSerializer(serializers.ModelSerializer):
    assigned_to_name = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()

    class Meta:
        model = SalesFollowupTask
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_assigned_to_name(self, obj):
        u = obj.assigned_to_user
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_lead_name(self, obj):
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()
