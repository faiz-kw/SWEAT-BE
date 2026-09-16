"""
apps/tenant_core/serializers_appointments.py — Serializers for Layer 2 Module F: Individual Appointments
"""

from rest_framework import serializers
from .models_appointments import (
    AppointmentType,
    AppointmentTypeSpecialtyRequirement,
    Appointment,
    AppointmentTrainer,
)


class AppointmentTypeSpecialtyRequirementSerializer(serializers.ModelSerializer):
    specialty_name = serializers.ReadOnlyField(source='trainer_specialty.name')
    specialty_code = serializers.ReadOnlyField(source='trainer_specialty.code')

    class Meta:
        model = AppointmentTypeSpecialtyRequirement
        fields = [
            'id', 'appointment_type', 'trainer_specialty',
            'specialty_name', 'specialty_code',
            'minimum_proficiency_level', 'is_mandatory', 'status',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AppointmentTypeSerializer(serializers.ModelSerializer):
    specialty_requirements = AppointmentTypeSpecialtyRequirementSerializer(many=True, read_only=True)

    class Meta:
        model = AppointmentType
        fields = [
            'id', 'organization', 'code', 'name', 'description',
            'default_duration_minutes', 'default_delivery_mode',
            'requires_trainer', 'status', 'specialty_requirements',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class AppointmentTrainerSerializer(serializers.ModelSerializer):
    trainer_name = serializers.SerializerMethodField()
    trainer_code = serializers.ReadOnlyField(source='trainer_profile.trainer_code')

    class Meta:
        model = AppointmentTrainer
        fields = [
            'id', 'appointment', 'trainer_profile', 'trainer_name',
            'trainer_code', 'role', 'status', 'assigned_by_user',
            'assigned_at', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'assigned_at', 'created_at', 'updated_at']

    def get_trainer_name(self, obj):
        if obj.trainer_profile and obj.trainer_profile.employee_profile and obj.trainer_profile.employee_profile.user_profile:
            up = obj.trainer_profile.employee_profile.user_profile
            return f"{up.first_name_snapshot or ''} {up.last_name_snapshot or ''}".strip()
        return ''


class AppointmentSerializer(serializers.ModelSerializer):
    appointment_type_name = serializers.ReadOnlyField(source='appointment_type.name')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    member_name = serializers.SerializerMethodField()
    member_number = serializers.ReadOnlyField(source='user_profile.member_number')
    assigned_trainers = AppointmentTrainerSerializer(many=True, read_only=True)

    class Meta:
        model = Appointment
        fields = [
            'id', 'appointment_type', 'appointment_type_name',
            'user_profile', 'member_name', 'member_number',
            'branch', 'branch_name', 'membership_id', 'entitlement_id',
            'start_at', 'end_at', 'delivery_mode', 'online_join_url',
            'status', 'booking_source', 'notes', 'created_by_user',
            'assigned_trainers', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_member_name(self, obj):
        if obj.user_profile:
            return f"{obj.user_profile.first_name_snapshot or ''} {obj.user_profile.last_name_snapshot or ''}".strip()
        return ''
