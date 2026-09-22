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
    LeadCommercialProfile,
    CRMStageSlaPolicy,
    CRMTrialReminderPolicy,
    CRMAgentAssignmentConfig,
    LeadAttribution,
)
from .models_attention import CRMAttentionPolicy
import re
from django.utils import timezone
from django.core.validators import validate_email, URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from .models_users import TenantUser


class LeadSourceSerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True)
    source_type = serializers.CharField(required=False, default='OTHER')
    status = serializers.CharField(required=False, default='ACTIVE')

    class Meta:
        model = LeadSource
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'organization']


class LeadCommercialProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadCommercialProfile
        fields = ['id', 'billing_name', 'gst_number', 'pan_number', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class LeadAttributionSerializer(serializers.ModelSerializer):
    lead_source_name = serializers.CharField(source='lead_source.name', read_only=True)

    class Meta:
        model = LeadAttribution
        fields = [
            'id', 'organization', 'lead', 'lead_source', 'lead_source_name',
            'touch_type', 'platform',
            'campaign_name', 'campaign_external_id',
            'ad_set_name', 'ad_set_external_id',
            'ad_name', 'ad_external_id',
            'form_name', 'form_external_id',
            'external_lead_id',
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'landing_page_url', 'referrer_url',
            'capture_method', 'captured_at', 'raw_metadata', 'created_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at']


class LeadSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    source_name = serializers.CharField(source='lead_source.name', read_only=True)
    interested_program_name = serializers.CharField(source='interested_program.name', read_only=True)
    assigned_sales_name = serializers.SerializerMethodField()
    referred_by_user_name = serializers.SerializerMethodField()

    # Commercial profile fields (accept directly or inside commercial_profile)
    commercial_profile = LeadCommercialProfileSerializer(read_only=True)
    billing_name = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    gst_number = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    pan_number = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    # Attribution & SLA calculated representations
    attributions = LeadAttributionSerializer(many=True, read_only=True)
    latest_attribution = serializers.SerializerMethodField()
    sla = serializers.SerializerMethodField()
    last_activity = serializers.SerializerMethodField()
    attention = serializers.SerializerMethodField()

    # Optional attribution input payload on lead intake
    attribution = serializers.DictField(required=False, write_only=True)

    # Aliases to make intake seamless
    email = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)
    location = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)
    goal = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)

    class Meta:
        model = Lead
        fields = '__all__'
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Flatten commercial details
        cp = getattr(instance, 'commercial_profile', None)
        if cp:
            data['billing_name'] = cp.billing_name
            data['gst_number'] = cp.gst_number
            data['pan_number'] = cp.pan_number
        else:
            data['billing_name'] = None
            data['gst_number'] = None
            data['pan_number'] = None
        # Provide convenient frontend aliases
        data['email'] = instance.email_normalized
        data['phone'] = instance.phone_normalized
        data['location'] = instance.area
        data['goal'] = instance.fitness_goal
        return data

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    def get_assigned_sales_name(self, obj):
        u = obj.assigned_sales_user
        if not u:
            return None
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_referred_by_user_name(self, obj):
        u = obj.referred_by_user
        if not u:
            return None
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_latest_attribution(self, obj):
        attributions = getattr(obj, '_prefetched_objects_cache', {}).get('attributions')
        if attributions is not None:
            sorted_attrs = sorted(attributions, key=lambda a: (a.captured_at, a.created_at), reverse=True)
            return LeadAttributionSerializer(sorted_attrs[0]).data if sorted_attrs else None
        latest = obj.attributions.all().order_by('-captured_at', '-created_at').first()
        if latest:
            return LeadAttributionSerializer(latest).data
        return None

    def get_sla(self, obj):
        from .services_crm import CRMLeadService
        db_alias = getattr(getattr(obj, '_state', None), 'db', None)
        return CRMLeadService.calculate_lead_sla(obj, db_alias=db_alias)

    def get_last_activity(self, obj):
        alias = getattr(getattr(obj, '_state', None), 'db', None) or 'default'
        latest = LeadActivity.objects.using(alias).filter(lead=obj).order_by('-activity_at').first()
        if not latest:
            return None
        return {
            'activity_type': latest.activity_type,
            'outcome': latest.outcome,
            'activity_at': latest.activity_at.isoformat() if latest.activity_at else None,
        }

    def get_attention(self, obj):
        # If pre-computed on obj (e.g. bulk evaluation in viewset), reuse it directly
        cached = getattr(obj, '_attention_summary', None)
        if cached is not None:
            return cached
        from .services_attention import LeadAttentionService
        db_alias = getattr(getattr(obj, '_state', None), 'db', None)
        full = LeadAttentionService.evaluate_lead(obj, db_alias=db_alias)
        return {
            'is_stuck': full['is_stuck'],
            'primary_reason': full['primary_reason'],
            'primary_reason_display': full['primary_reason_display'],
            'severity': full['severity'],
            'overdue_by_seconds': full['overdue_by_seconds'],
            'stage_age_seconds': full['stage_age_seconds'],
            'recommended_action': full['recommended_action'],
        }

    def validate_first_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("First name is required.")
        return value.strip()

    def validate_date_of_birth(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Birthday cannot be a future date.")
        return value

    def validate_gst_number(self, value):
        if not value:
            return value
        cleaned = value.strip().upper()
        # If 13 characters matching PAN + 1Z5 (missing 2-digit state code), auto-prefix state code '27'
        if re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$', cleaned):
            cleaned = f"27{cleaned}"
        # Validate that it matches standard 15-char Indian GST or valid alphanumeric business tax ID
        gst_regex = r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
        if not re.match(gst_regex, cleaned) and not re.match(r'^[A-Z0-9]{5,20}$', cleaned):
            raise serializers.ValidationError("Invalid GST format (must be 15 alphanumeric characters, e.g. 27AAAAA0000A1Z5, or valid tax identifier).")
        return cleaned

    def validate_pan_number(self, value):
        if not value:
            return value
        cleaned = value.strip().upper()
        # Standard Indian PAN pattern: 5 letters, 4 digits, 1 letter, or general alphanumeric tax ID
        pan_regex = r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$'
        if not re.match(pan_regex, cleaned) and not re.match(r'^[A-Z0-9]{5,15}$', cleaned):
            raise serializers.ValidationError("Invalid PAN format (e.g. ABCDE1234F).")
        return cleaned

    def validate(self, attrs):
        # Resolve aliases
        if 'email' in attrs and not attrs.get('email_normalized'):
            attrs['email_normalized'] = attrs.pop('email')
        if 'phone' in attrs and not attrs.get('phone_normalized'):
            attrs['phone_normalized'] = attrs.pop('phone')
        if 'location' in attrs and not attrs.get('area'):
            attrs['area'] = attrs.pop('location')
        if 'goal' in attrs and not attrs.get('fitness_goal'):
            attrs['fitness_goal'] = attrs.pop('goal')

        from .services_crm import validate_lead_email, validate_lead_phone

        email = attrs.get('email_normalized')
        if email:
            try:
                attrs['email_normalized'] = validate_lead_email(email)
            except DjangoValidationError as err:
                raise serializers.ValidationError({'email': 'Please enter a valid Gmail address.'})

        phone = attrs.get('phone_normalized')
        if phone:
            try:
                attrs['phone_normalized'] = validate_lead_phone(phone, required=True)
            except DjangoValidationError as err:
                raise serializers.ValidationError({'phone': 'Please enter a valid 10-digit mobile number.'})

        # Validate lead source is active if provided
        lead_source = attrs.get('lead_source')
        if lead_source and hasattr(lead_source, 'status') and lead_source.status != 'ACTIVE':
            raise serializers.ValidationError({'lead_source': 'Selected lead source is not active.'})

        # Validate branch is active if provided
        branch = attrs.get('branch')
        if branch and hasattr(branch, 'status') and branch.status != 'ACTIVE':
            raise serializers.ValidationError({'branch': 'Selected branch is not active.'})

        # Validate interested program is active if provided
        program = attrs.get('interested_program')
        if program and hasattr(program, 'status') and program.status != 'ACTIVE':
            raise serializers.ValidationError({'interested_program': 'Selected program is not active.'})

        # Validate assigned agent is active if provided
        assigned_agent = attrs.get('assigned_sales_user')
        if assigned_agent and hasattr(assigned_agent, 'status') and assigned_agent.status != 'ACTIVE':
            raise serializers.ValidationError({'assigned_sales_user': 'Selected agent is not active.'})

        # Validate attribution payload if supplied
        attr_data = attrs.get('attribution')
        if attr_data and isinstance(attr_data, dict):
            has_meaningful_data = any(
                bool(str(v).strip()) for k, v in attr_data.items()
                if k not in ('touch_type', 'raw_metadata') and v is not None
            )
            if not has_meaningful_data:
                attrs.pop('attribution', None)
            else:
                url_validator = URLValidator()
                for url_field in ('landing_page_url', 'referrer_url'):
                    val = attr_data.get(url_field)
                    if val:
                        try:
                            url_validator(val)
                        except DjangoValidationError:
                            raise serializers.ValidationError({f'attribution.{url_field}': f'Invalid URL format for {url_field}.'})
            # Ensure reasonable string length bounds
            for str_field in ('campaign_name', 'ad_set_name', 'ad_name', 'form_name', 'external_lead_id', 'utm_source', 'utm_medium', 'utm_campaign'):
                val = attr_data.get(str_field)
                if val and len(str(val)) > 255:
                    raise serializers.ValidationError({f'attribution.{str_field}': f'{str_field} cannot exceed 255 characters.'})

        return attrs


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
    lead_name = serializers.SerializerMethodField()
    lead_phone = serializers.SerializerMethodField()
    lead_status = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()

    class Meta:
        model = LeadActivity
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_performed_by_name(self, obj):
        u = obj.performed_by_user
        if not u:
            return 'System'
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_lead_name(self, obj):
        if not obj.lead:
            return ''
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()

    def get_lead_phone(self, obj):
        return obj.lead.phone_normalized if obj.lead else ''

    def get_lead_status(self, obj):
        return obj.lead.current_status if obj.lead else ''

    def get_branch_name(self, obj):
        if obj.lead and obj.lead.branch:
            return obj.lead.branch.name
        return ''


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
    lead_phone = serializers.SerializerMethodField()
    lead_email = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    trainer_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    is_rescheduled = serializers.SerializerMethodField()

    class Meta:
        model = TrialBooking
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by_user']

    def get_lead_name(self, obj):
        if not obj.lead:
            return ''
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()

    def get_lead_phone(self, obj):
        return obj.lead.phone_normalized if obj.lead else ''

    def get_lead_email(self, obj):
        return obj.lead.email_normalized if obj.lead else ''

    def get_trainer_name(self, obj):
        tp = obj.assigned_trainer_profile
        if not tp:
            return 'Unassigned'
        if tp.user:
            return f"{tp.user.first_name} {tp.user.last_name}".strip() or tp.trainer_code
        return tp.trainer_code

    def get_class_name(self, obj):
        if not obj.class_occurrence_id:
            return obj.trial_type or 'General Trial'
        alias = obj._state.db or 'default'
        from .models_classes import ClassOccurrence
        occ = ClassOccurrence.objects.using(alias).filter(id=obj.class_occurrence_id).select_related('class_template').first()
        return occ.class_template.name if occ and occ.class_template else obj.trial_type

    def get_is_rescheduled(self, obj):
        alias = obj._state.db or 'default'
        return TrialBooking.objects.using(alias).filter(rescheduled_from=obj).exists()


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
    created_by_name = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()
    lead_phone = serializers.SerializerMethodField()
    lead_email = serializers.SerializerMethodField()
    lead_status = serializers.SerializerMethodField()
    branch_id = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    last_activity = serializers.SerializerMethodField()
    assigned_to_user = serializers.PrimaryKeyRelatedField(
        queryset=TenantUser.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = SalesFollowupTask
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by_user']

    def validate_priority(self, value):
        if value == 'MEDIUM':
            return 'NORMAL'
        return value

    def validate_assigned_to_user(self, value):
        if value:
            if getattr(value, 'status', None) == 'INACTIVE' or not getattr(value, 'is_active', True):
                raise serializers.ValidationError("Cannot assign tasks to an inactive agent.")
        return value

    def get_assigned_to_name(self, obj):
        u = obj.assigned_to_user
        if not u:
            return 'Unassigned'
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_created_by_name(self, obj):
        u = obj.created_by_user
        if not u:
            return 'System'
        return f"{u.first_name} {u.last_name}".strip() or u.email

    def get_lead_name(self, obj):
        if not obj.lead:
            return ''
        return f"{obj.lead.first_name} {obj.lead.last_name}".strip()

    def get_lead_phone(self, obj):
        return obj.lead.phone_normalized if obj.lead else ''

    def get_lead_email(self, obj):
        return obj.lead.email_normalized if obj.lead else ''

    def get_lead_status(self, obj):
        return obj.lead.current_status if obj.lead else ''

    def get_branch_id(self, obj):
        if obj.lead and obj.lead.branch_id:
            return str(obj.lead.branch_id)
        return None

    def get_branch_name(self, obj):
        if obj.lead and obj.lead.branch:
            return obj.lead.branch.name
        return ''

    def get_is_overdue(self, obj):
        if obj.status in ('PENDING', 'IN_PROGRESS') and obj.due_at:
            return obj.due_at < timezone.now()
        return False

    def get_last_activity(self, obj):
        if not obj.lead_id:
            return None
        alias = getattr(getattr(obj, '_state', None), 'db', None) or 'default'
        latest = LeadActivity.objects.using(alias).filter(lead=obj.lead).order_by('-activity_at').first()
        if not latest:
            return None
        return {
            'activity_type': latest.activity_type,
            'outcome': latest.outcome,
            'activity_at': latest.activity_at.isoformat() if latest.activity_at else None,
        }


class CRMStageSlaPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = CRMStageSlaPolicy
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'organization']


class CRMTrialReminderPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = CRMTrialReminderPolicy
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'organization']


class CRMAttentionPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = CRMAttentionPolicy
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'organization']


class CRMAgentAssignmentConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = CRMAgentAssignmentConfig
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'organization']

