from django.utils import timezone
from rest_framework import serializers
from .models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule, ClassSpecialtyRequirement,
    ClassScheduleImportBatch, ClassScheduleImportRow,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation,
)


from .serializers_catalog import _generate_unique_code


class ClassCategorySerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)

    class Meta:
        model = ClassCategory
        fields = ['id', 'organization', 'code', 'name', 'description', 'display_order', 'status', 'created_at', 'updated_at']
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
        }
        validators = []

    def create(self, validated_data):
        if not validated_data.get('code'):
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias')
            org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
            validated_data['code'] = _generate_unique_code(ClassCategory, org, validated_data.get('name', 'CATEGORY'), db_alias=alias)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        return super().update(instance, validated_data)


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
    version_number = serializers.IntegerField(required=False)
    effective_from = serializers.DateField(required=False)

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
    code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    name = serializers.CharField(required=False, allow_blank=True, max_length=200)
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
        extra_kwargs = {
            'code': {'required': False, 'allow_blank': True},
            'name': {'required': False, 'allow_blank': True},
            'min_age': {'required': False, 'allow_null': True},
            'max_age': {'required': False, 'allow_null': True},
        }
        validators = []

    def validate(self, attrs):
        if not attrs.get('name'):
            category = attrs.get('category')
            if category:
                attrs['name'] = category.name
            elif self.instance and self.instance.category:
                attrs['name'] = self.instance.category.name
            elif self.instance and self.instance.name:
                attrs['name'] = self.instance.name
            else:
                raise serializers.ValidationError({'category': 'Please select a Class Name.'})
        return attrs

    def create(self, validated_data):
        if not validated_data.get('name'):
            cat = validated_data.get('category')
            if cat:
                validated_data['name'] = cat.name
        if not validated_data.get('code'):
            req = self.context.get('request')
            alias = getattr(getattr(req, 'user', None), '_db_alias', None) or self.context.get('db_alias')
            org = validated_data.get('organization') or getattr(req, 'organization', None) or self.context.get('organization')
            validated_data['code'] = _generate_unique_code(ClassTemplate, org, validated_data.get('name', 'CLASS'), db_alias=alias)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if 'code' in validated_data and not validated_data['code']:
            validated_data.pop('code')
        if not validated_data.get('name') and validated_data.get('category'):
            validated_data['name'] = validated_data['category'].name
        return super().update(instance, validated_data)


class ClassScheduleRuleSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_template.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    recurrence_type = serializers.CharField(required=False, default='WEEKLY')
    delivery_mode = serializers.CharField(required=False, default='OFFLINE')
    status = serializers.CharField(required=False, default='ACTIVE')

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

    def validate(self, attrs):
        start_time = attrs.get('start_time') or (self.instance.start_time if self.instance else None)
        end_time = attrs.get('end_time') or (self.instance.end_time if self.instance else None)
        branch = attrs.get('branch') or (self.instance.branch if self.instance else None)
        days_of_week = attrs.get('days_of_week') if 'days_of_week' in attrs else (self.instance.days_of_week if self.instance else [])
        valid_from = attrs.get('valid_from') or (self.instance.valid_from if self.instance else None)
        valid_until = attrs.get('valid_until') if 'valid_until' in attrs else (self.instance.valid_until if self.instance else None)

        if not attrs.get('recurrence_type'):
            attrs['recurrence_type'] = 'WEEKLY'
        if not attrs.get('delivery_mode'):
            attrs['delivery_mode'] = 'OFFLINE'
        if not attrs.get('status'):
            attrs['status'] = 'ACTIVE'

        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError({"end_time": "End time must be strictly greater than start time."})

        if valid_from and valid_until and valid_until < valid_from:
            raise serializers.ValidationError({"valid_until": "Valid until date must be on or after valid from date."})

        if valid_until and valid_until < timezone.now().date():
            raise serializers.ValidationError({"valid_until": "Recurring schedule period cannot be entirely in the past."})

        if branch:
            if getattr(branch, 'status', None) and branch.status != 'ACTIVE':
                raise serializers.ValidationError({"branch": f"Branch '{branch.name}' is not ACTIVE."})

            # Check branch working hours
            from .models_govern import BranchWorkingHours, BranchOperatingException
            from .context import get_tenant_db_alias
            alias = get_tenant_db_alias() or 'default'
            day_names = {1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday', 7: 'Sunday'}
            for dow in (days_of_week or []):
                wh = BranchWorkingHours.objects.using(alias).filter(branch=branch, day_of_week=dow).first()
                if wh:
                    d_name = day_names.get(dow, f'Day {dow}')
                    if not wh.is_open:
                        raise serializers.ValidationError({"branch": f"Branch '{branch.name}' is closed on {d_name}."})
                    if not wh.is_24_hours:
                        if wh.open_time and start_time and start_time < wh.open_time:
                            raise serializers.ValidationError(
                                {"start_time": f"Class start time ({start_time.strftime('%H:%M')}) is before branch opening time ({wh.open_time.strftime('%H:%M')}) on {d_name}."}
                            )
                        if wh.close_time and end_time and end_time > wh.close_time:
                            raise serializers.ValidationError(
                                {"end_time": f"Class end time ({end_time.strftime('%H:%M')}) is after branch closing time ({wh.close_time.strftime('%H:%M')}) on {d_name}."}
                            )

            # Check holiday exceptions when valid_from is single date or short range
            if valid_from:
                end_check = valid_until or valid_from
                # If exact single day rule falls on a closed holiday
                if valid_from == end_check:
                    holiday = BranchOperatingException.objects.using(alias).filter(
                        branch=branch, exception_date=valid_from, is_closed=True
                    ).first()
                    if holiday:
                        raise serializers.ValidationError({
                            "valid_from": f"Branch '{branch.name}' is closed on {valid_from} ({holiday.reason or 'Holiday/Maintenance'})."
                        })

        return attrs


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
    branch_latitude = serializers.DecimalField(source='branch.latitude', max_digits=10, decimal_places=7, read_only=True)
    branch_longitude = serializers.DecimalField(source='branch.longitude', max_digits=10, decimal_places=7, read_only=True)
    branch_geofence_radius_meters = serializers.IntegerField(source='branch.geofence_radius_meters', read_only=True)
    branch_geofence_enforcement = serializers.CharField(source='branch.geofence_enforcement', read_only=True)
    trainers = ClassOccurrenceTrainerSerializer(source='trainer_assignments', many=True, read_only=True)
    active_content = serializers.SerializerMethodField()
    trainer_checked_in = serializers.SerializerMethodField()
    trainer_check_in_details = serializers.SerializerMethodField()
    booking_count = serializers.SerializerMethodField()
    waitlist_count = serializers.SerializerMethodField()
    start_time = serializers.TimeField(write_only=True, required=False)
    end_time = serializers.TimeField(write_only=True, required=False)

    def get_trainer_checked_in(self, obj):
        try:
            return any(t.status == 'CONFIRMED' for t in obj.trainer_assignments.all())
        except Exception:
            return False

    def get_trainer_check_in_details(self, obj):
        try:
            confirmed = next((t for t in obj.trainer_assignments.all() if t.status == 'CONFIRMED'), None)
            if confirmed:
                t_name = None
                if confirmed.trainer_profile and getattr(confirmed.trainer_profile, 'employee_profile', None):
                    up = getattr(confirmed.trainer_profile.employee_profile, 'user_profile', None)
                    if up and getattr(up, 'user', None):
                        t_name = up.user.full_name or up.user.email
                if not t_name and confirmed.trainer_profile:
                    t_name = confirmed.trainer_profile.trainer_code
                return {
                    'checked_in': True,
                    'trainer_name': t_name or 'Trainer',
                    'status': 'CONFIRMED',
                }
            assigned = next((t for t in obj.trainer_assignments.all() if t.status in ['ASSIGNED', 'CONFIRMED']), None)
            if assigned:
                t_name = None
                if assigned.trainer_profile and getattr(assigned.trainer_profile, 'employee_profile', None):
                    up = getattr(assigned.trainer_profile.employee_profile, 'user_profile', None)
                    if up and getattr(up, 'user', None):
                        t_name = up.user.full_name or up.user.email
                if not t_name and assigned.trainer_profile:
                    t_name = assigned.trainer_profile.trainer_code
                return {
                    'checked_in': False,
                    'trainer_name': t_name or 'Trainer',
                    'status': 'PENDING',
                }
            return {
                'checked_in': False,
                'trainer_name': None,
                'status': 'UNASSIGNED',
            }
        except Exception:
            return {'checked_in': False, 'status': 'UNKNOWN'}

    def get_booking_count(self, obj):
        try:
            return sum(1 for b in obj.bookings.all() if b.status in ['CONFIRMED', 'RESERVED', 'COMPLETED'])
        except Exception:
            return 0

    def get_waitlist_count(self, obj):
        try:
            return sum(1 for b in obj.bookings.all() if b.status == 'WAITLISTED')
        except Exception:
            return 0

    def get_active_content(self, obj):
        try:
            latest = obj.content_assignments.filter(status='ACTIVE').select_related('content_item').order_by('-assigned_at').first()
            if latest and latest.content_item:
                return {
                    'id': str(latest.id),
                    'title': latest.content_item.title,
                    'content_type': latest.content_item.content_type,
                    'external_url': latest.content_item.external_url,
                    'rotation_cycle_number': latest.rotation_cycle_number,
                }
        except Exception:
            pass
        return None

    class Meta:
        model = ClassOccurrence
        fields = [
            'id', 'class_template', 'class_name', 'schedule_rule', 'branch', 'branch_name',
            'branch_latitude', 'branch_longitude', 'branch_geofence_radius_meters', 'branch_geofence_enforcement',
            'occurrence_date', 'start_at', 'end_at', 'start_time', 'end_time', 'delivery_mode',
            'online_provider', 'online_join_url', 'capacity', 'trial_capacity',
            'waitlist_capacity', 'booking_open_at', 'booking_close_at',
            'cancellation_cutoff_at', 'status', 'is_manual', 'is_override',
            'trainers', 'trainer_checked_in', 'trainer_check_in_details', 'booking_count', 'waitlist_count',
            'active_content', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'start_at': {'required': False},
            'end_at': {'required': False},
            'occurrence_date': {'required': False},
            'capacity': {'required': False, 'allow_null': True},
            'trial_capacity': {'required': False, 'allow_null': True},
            'waitlist_capacity': {'required': False, 'allow_null': True},
        }

    def validate(self, attrs):
        start_time = attrs.pop('start_time', None)
        end_time = attrs.pop('end_time', None)
        occ_date = attrs.get('occurrence_date')
        tpl = attrs.get('class_template') or (self.instance.class_template if self.instance else None)
        branch = attrs.get('branch') or (self.instance.branch if self.instance else None)

        if start_time and end_time and occ_date:
            if end_time <= start_time:
                raise serializers.ValidationError({"end_time": "end_time must be strictly greater than start_time."})
            import datetime
            from django.utils import timezone
            attrs['start_at'] = timezone.make_aware(datetime.datetime.combine(occ_date, start_time))
            attrs['end_at'] = timezone.make_aware(datetime.datetime.combine(occ_date, end_time))

        start_at = attrs.get('start_at') or (self.instance.start_at if self.instance else None)
        end_at = attrs.get('end_at') or (self.instance.end_at if self.instance else None)

        if start_at and end_at and end_at <= start_at:
            raise serializers.ValidationError({"end_at": "end_at must be strictly greater than start_at."})

        if start_at and not occ_date:
            attrs['occurrence_date'] = start_at.date()
            occ_date = start_at.date()

        if tpl:
            if attrs.get('capacity') is None:
                attrs['capacity'] = tpl.default_capacity
            if attrs.get('trial_capacity') is None:
                attrs['trial_capacity'] = tpl.default_trial_capacity
            if attrs.get('waitlist_capacity') is None:
                attrs['waitlist_capacity'] = tpl.default_waitlist_capacity
            if 'delivery_mode' not in attrs or not attrs.get('delivery_mode'):
                attrs['delivery_mode'] = tpl.default_delivery_mode
        else:
            if attrs.get('capacity') is None:
                attrs['capacity'] = 20
            if attrs.get('trial_capacity') is None:
                attrs['trial_capacity'] = 0
            if attrs.get('waitlist_capacity') is None:
                attrs['waitlist_capacity'] = 0

        if branch and occ_date and start_at and end_at:
            from .services_schedule import BranchScheduleService
            from .context import get_tenant_db_alias
            alias = get_tenant_db_alias() or 'default'
            eff = BranchScheduleService.get_effective_schedule_for_date(branch, occ_date, alias)
            if not eff.get('is_open', True):
                raise serializers.ValidationError({"branch": f"Branch '{branch.name}' is closed on {occ_date} ({eff.get('reason') or 'Closed'})."})
            if not eff.get('is_24_hours', False):
                st_time = start_at.time()
                et_time = end_at.time()
                if eff.get('open_time') and st_time < eff['open_time']:
                    raise serializers.ValidationError({"start_at": f"Class start time {st_time.strftime('%H:%M')} is before branch opening time {eff['open_time'].strftime('%H:%M')}."})
                if eff.get('close_time') and et_time > eff['close_time']:
                    raise serializers.ValidationError({"end_at": f"Class end time {et_time.strftime('%H:%M')} is after branch closing time {eff['close_time'].strftime('%H:%M')}."})

        return attrs


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
