"""
apps/tenant_core/serializers_workforce.py — DRF Serializers for Layer 2 Module A: Workforce & Trainers
"""

from rest_framework import serializers
from .models_workforce import (
    UserProfile,
    EmployeeProfile,
    TrainerProfile,
    SalesProfile,
    EmployeeWorkSchedule,
    EmployeeScheduleException,
    TrainerSpecialty,
    TrainerSpecialtyAssignment,
)


class UserProfileSerializer(serializers.ModelSerializer):
    email = serializers.CharField(source='user.email', read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = UserProfile
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_full_name(self, obj):
        first = obj.first_name_snapshot or getattr(obj.user, 'first_name', '')
        last = obj.last_name_snapshot or getattr(obj.user, 'last_name', '')
        return f"{first} {last}".strip()


class EmployeeProfileSerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user_profile.user.email', read_only=True)

    class Meta:
        model = EmployeeProfile
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_employee_name(self, obj):
        up = obj.user_profile
        first = up.first_name_snapshot or getattr(up.user, 'first_name', '')
        last = up.last_name_snapshot or getattr(up.user, 'last_name', '')
        return f"{first} {last}".strip()


class TrainerSpecialtySerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainerSpecialty
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class TrainerSpecialtyAssignmentSerializer(serializers.ModelSerializer):
    specialty_code = serializers.CharField(source='trainer_specialty.code', read_only=True)
    specialty_name = serializers.CharField(source='trainer_specialty.name', read_only=True)
    trainer_code = serializers.CharField(source='trainer_profile.trainer_code', read_only=True)

    class Meta:
        model = TrainerSpecialtyAssignment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class TrainerProfileSerializer(serializers.ModelSerializer):
    trainer_name = serializers.SerializerMethodField()
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True)
    email = serializers.CharField(source='employee_profile.user_profile.user.email', read_only=True)
    user_id = serializers.UUIDField(source='employee_profile.user_profile.user.id', read_only=True)
    user = serializers.UUIDField(source='employee_profile.user_profile.user.id', read_only=True)
    specialties = serializers.SerializerMethodField()

    branch_ids = serializers.SerializerMethodField()
    branch_names = serializers.SerializerMethodField()

    category_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=False,
    )
    categories = serializers.SerializerMethodField()
    category_names = serializers.SerializerMethodField()

    class Meta:
        model = TrainerProfile
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_trainer_name(self, obj):
        up = obj.employee_profile.user_profile
        first = up.first_name_snapshot or getattr(up.user, 'first_name', '')
        last = up.last_name_snapshot or getattr(up.user, 'last_name', '')
        return f"{first} {last}".strip()

    def get_branch_ids(self, obj):
        alias = obj._state.db or 'default'
        user = obj.employee_profile.user_profile.user
        b_ids = set()
        if user.home_branch_id:
            b_ids.add(str(user.home_branch_id))
        prefetched_branches = getattr(user, '_prefetched_objects_cache', {}).get('branch_assignments')
        if prefetched_branches is not None:
            assignments = [a for a in prefetched_branches if a.status == 'ACTIVE']
        else:
            assignments = user.branch_assignments.using(alias).filter(status='ACTIVE')
        for a in assignments:
            b_ids.add(str(a.branch_id))
        pref = obj.employee_profile.user_profile.preferred_branch_id
        if pref:
            b_ids.add(str(pref))

        # Include direct user branch clearances
        try:
            from .models_users import UserBranch
            for ub in UserBranch.objects.using(alias).filter(user=user, is_active=True):
                b_ids.add(str(ub.branch_id))
        except Exception:
            pass

        # Include any branches where the trainer has scheduled shifts
        try:
            for ws_bid in obj.employee_profile.work_schedules.using(alias).filter(status='ACTIVE').values_list('branch_id', flat=True):
                b_ids.add(str(ws_bid))
        except Exception:
            pass

        return list(b_ids)

    def get_branch_names(self, obj):
        alias = obj._state.db or 'default'
        user = obj.employee_profile.user_profile.user
        names = []
        if user.home_branch:
            names.append(user.home_branch.name)
        prefetched_branches = getattr(user, '_prefetched_objects_cache', {}).get('branch_assignments')
        if prefetched_branches is not None:
            assignments = [a for a in prefetched_branches if a.status == 'ACTIVE']
        else:
            assignments = user.branch_assignments.using(alias).filter(status='ACTIVE').select_related('branch')
        for a in assignments:
            if a.branch and a.branch.name not in names:
                names.append(a.branch.name)

        try:
            from .models_users import UserBranch
            for ub in UserBranch.objects.using(alias).filter(user=user, is_active=True).select_related('branch'):
                if ub.branch and ub.branch.name not in names:
                    names.append(ub.branch.name)
        except Exception:
            pass

        try:
            for ws in obj.employee_profile.work_schedules.using(alias).filter(status='ACTIVE').select_related('branch'):
                if ws.branch and ws.branch.name not in names:
                    names.append(ws.branch.name)
        except Exception:
            pass

        return names

    def get_specialties(self, obj):
        alias = obj._state.db or 'default'
        prefetched_specs = getattr(obj, '_prefetched_objects_cache', {}).get('specialty_assignments')
        if prefetched_specs is not None:
            assignments = [a for a in prefetched_specs if a.status == 'ACTIVE']
        else:
            assignments = obj.specialty_assignments.using(alias).filter(status='ACTIVE').select_related('trainer_specialty')
        return [
            {
                'id': str(a.id),
                'code': a.trainer_specialty.code,
                'name': a.trainer_specialty.name,
                'proficiency_level': a.proficiency_level,
                'allow_group': a.allow_group,
                'allow_individual': a.allow_individual,
                'allow_online': a.allow_online,
                'is_primary': a.is_primary,
            }
            for a in assignments
        ]

    def get_categories(self, obj):
        alias = obj._state.db or 'default'
        prefetched = getattr(obj, '_prefetched_objects_cache', {}).get('category_assignments')
        if prefetched is not None:
            assignments = prefetched
        else:
            assignments = obj.category_assignments.using(alias).select_related('class_category')
        return [
            {
                'id': str(a.class_category.id),
                'name': a.class_category.name,
                'code': a.class_category.code,
                'status': a.class_category.status,
                'display_order': a.class_category.display_order,
            }
            for a in assignments
            if a.class_category
        ]

    def get_category_names(self, obj):
        alias = obj._state.db or 'default'
        prefetched = getattr(obj, '_prefetched_objects_cache', {}).get('category_assignments')
        if prefetched is not None:
            return [a.class_category.name for a in prefetched if a.class_category]
        return list(
            obj.category_assignments.using(alias)
            .filter(class_category__isnull=False)
            .values_list('class_category__name', flat=True)
        )

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['category_ids'] = [str(c['id']) for c in ret.get('categories', [])]
        return ret

    def create(self, validated_data):
        category_ids = validated_data.pop('category_ids', None)
        instance = super().create(validated_data)
        if category_ids is not None:
            self._sync_categories(instance, category_ids)
        return instance

    def update(self, instance, validated_data):
        category_ids = validated_data.pop('category_ids', None)
        instance = super().update(instance, validated_data)
        if category_ids is not None:
            self._sync_categories(instance, category_ids)
        return instance

    def _sync_categories(self, instance, category_ids):
        from django.db import transaction
        from .models_workforce import TrainerClassCategoryAssignment
        alias = instance._state.db or 'default'
        with transaction.atomic(using=alias):
            # Exclude and delete categories not in the new list
            instance.category_assignments.using(alias).exclude(class_category_id__in=category_ids).delete()
            # Fetch already linked category IDs
            existing_cids = set(
                instance.category_assignments.using(alias).values_list('class_category_id', flat=True)
            )
            to_add = [
                TrainerClassCategoryAssignment(trainer_profile=instance, class_category_id=cid)
                for cid in category_ids
                if cid not in existing_cids
            ]
            if to_add:
                TrainerClassCategoryAssignment.objects.using(alias).bulk_create(to_add)


class SalesProfileSerializer(serializers.ModelSerializer):
    sales_rep_name = serializers.SerializerMethodField()
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True)
    email = serializers.CharField(source='employee_profile.user_profile.user.email', read_only=True)
    user_id = serializers.UUIDField(source='employee_profile.user_profile.user.id', read_only=True)
    user = serializers.UUIDField(source='employee_profile.user_profile.user.id', read_only=True)

    class Meta:
        model = SalesProfile
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_sales_rep_name(self, obj):
        up = obj.employee_profile.user_profile
        first = up.first_name_snapshot or getattr(up.user, 'first_name', '')
        last = up.last_name_snapshot or getattr(up.user, 'last_name', '')
        return f"{first} {last}".strip()


class EmployeeWorkScheduleSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    employee_name = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeWorkSchedule
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_employee_name(self, obj):
        up = obj.employee_profile.user_profile
        first = up.first_name_snapshot or getattr(up.user, 'first_name', '')
        last = up.last_name_snapshot or getattr(up.user, 'last_name', '')
        return f"{first} {last}".strip()


class EmployeeScheduleExceptionSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True, allow_null=True)
    employee_name = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeScheduleException
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_employee_name(self, obj):
        up = obj.employee_profile.user_profile
        first = up.first_name_snapshot or getattr(up.user, 'first_name', '')
        last = up.last_name_snapshot or getattr(up.user, 'last_name', '')
        return f"{first} {last}".strip()
