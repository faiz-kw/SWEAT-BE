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
    specialties = serializers.SerializerMethodField()

    branch_ids = serializers.SerializerMethodField()
    branch_names = serializers.SerializerMethodField()

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
        assignments = user.branch_assignments.using(alias).filter(status='ACTIVE')
        for a in assignments:
            b_ids.add(str(a.branch_id))
        pref = obj.employee_profile.user_profile.preferred_branch_id
        if pref:
            b_ids.add(str(pref))
        return list(b_ids)

    def get_branch_names(self, obj):
        alias = obj._state.db or 'default'
        user = obj.employee_profile.user_profile.user
        names = []
        if user.home_branch:
            names.append(user.home_branch.name)
        assignments = user.branch_assignments.using(alias).filter(status='ACTIVE').select_related('branch')
        for a in assignments:
            if a.branch and a.branch.name not in names:
                names.append(a.branch.name)
        return names

    def get_specialties(self, obj):
        alias = obj._state.db or 'default'
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


class SalesProfileSerializer(serializers.ModelSerializer):
    sales_rep_name = serializers.SerializerMethodField()
    employee_code = serializers.CharField(source='employee_profile.employee_code', read_only=True)
    email = serializers.CharField(source='employee_profile.user_profile.user.email', read_only=True)

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
