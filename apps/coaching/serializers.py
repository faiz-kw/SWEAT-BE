"""
Serializers for Fitness Assessments, Exercise Library, Workout Programs, and Nutrition Plans.
"""

from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field, OpenApiTypes
from .models import (
    FitnessAssessment,
    Exercise,
    WorkoutProgram,
    WorkoutDay,
    WorkoutDayExercise,
    FoodItem,
    NutritionPlan,
    MealItem,
)
from apps.members.models import Member

# ---------------------------------------------------------------------------
# 1. Assessment Serializer
# ---------------------------------------------------------------------------

class FitnessAssessmentSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.name', read_only=True)
    assessed_by_name = serializers.CharField(source='assessed_by.full_name', read_only=True, default='')
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = FitnessAssessment
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'assessed_by',
            'assessed_by_name',
            'assessment_date',
            'assessment_type',
            'height_cm',
            'weight_kg',
            'waist_cm',
            'hip_cm',
            'chest_cm',
            'arm_cm',
            'thigh_cm',
            'bmi',
            'body_fat_pct',
            'muscle_mass_pct',
            'squat_1rm_kg',
            'bench_1rm_kg',
            'deadlift_1rm_kg',
            'plank_hold_sec',
            'mobility_score',
            'overall_fitness_score',
            'posture_notes',
            'trainer_summary',
            'created_at',
        ]
        read_only_fields = ['tenant_id', 'created_at']


# ---------------------------------------------------------------------------
# 2. Exercise Library Serializer
# ---------------------------------------------------------------------------

class ExerciseSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = Exercise
        fields = [
            'id',
            'tenant_id',
            'name',
            'category',
            'primary_muscle',
            'equipment',
            'difficulty',
            'movement_pattern',
            'instructions',
            'video_url',
            'contraindications',
        ]
        read_only_fields = ['tenant_id']


# ---------------------------------------------------------------------------
# 3. Workout Program Serializers
# ---------------------------------------------------------------------------

class WorkoutDayExerciseSerializer(serializers.ModelSerializer):
    exercise_name = serializers.CharField(source='exercise.name', read_only=True)
    category = serializers.CharField(source='exercise.category', read_only=True)
    primary_muscle = serializers.CharField(source='exercise.primary_muscle', read_only=True)

    class Meta:
        model = WorkoutDayExercise
        fields = [
            'id',
            'exercise',
            'exercise_name',
            'category',
            'primary_muscle',
            'order',
            'sets',
            'reps',
            'rest_seconds',
            'tempo',
            'notes',
        ]


class WorkoutDaySerializer(serializers.ModelSerializer):
    exercises = WorkoutDayExerciseSerializer(many=True, read_only=True)

    class Meta:
        model = WorkoutDay
        fields = [
            'id',
            'day_name',
            'day_order',
            'exercises',
        ]


class WorkoutProgramSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.name', read_only=True)
    trainer_name = serializers.CharField(source='trainer.full_name', read_only=True, default='')
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = WorkoutProgram
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'trainer',
            'trainer_name',
            'name',
            'goal',
            'duration_weeks',
            'days_per_week',
            'start_date',
            'end_date',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['tenant_id', 'created_at']


class WorkoutProgramDetailSerializer(WorkoutProgramSerializer):
    workout_days = WorkoutDaySerializer(many=True, read_only=True)

    class Meta(WorkoutProgramSerializer.Meta):
        fields = WorkoutProgramSerializer.Meta.fields + ['workout_days']


# ---------------------------------------------------------------------------
# 4. Nutrition & Indian Food Serializers
# ---------------------------------------------------------------------------

class FoodItemSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = FoodItem
        fields = [
            'id',
            'tenant_id',
            'name',
            'diet_type',
            'serving_unit',
            'calories',
            'protein_g',
            'carbs_g',
            'fat_g',
            'fiber_g',
        ]
        read_only_fields = ['tenant_id']


class MealItemSerializer(serializers.ModelSerializer):
    food_name = serializers.CharField(source='food_item.name', read_only=True)
    food_calories = serializers.DecimalField(source='food_item.calories', max_digits=6, decimal_places=1, read_only=True)
    food_protein = serializers.DecimalField(source='food_item.protein_g', max_digits=5, decimal_places=1, read_only=True)

    class Meta:
        model = MealItem
        fields = [
            'id',
            'meal_time',
            'food_item',
            'food_name',
            'food_calories',
            'food_protein',
            'quantity',
            'calculated_calories',
        ]


class NutritionPlanSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.name', read_only=True)
    trainer_name = serializers.CharField(source='trainer.full_name', read_only=True, default='')
    tenant_id = serializers.CharField(source='tenant.id', read_only=True)

    class Meta:
        model = NutritionPlan
        fields = [
            'id',
            'tenant_id',
            'member',
            'member_name',
            'trainer',
            'trainer_name',
            'name',
            'diet_type',
            'daily_calorie_target',
            'protein_target_g',
            'carbs_target_g',
            'fat_target_g',
            'hydration_liters',
            'notes',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['tenant_id', 'created_at']


class NutritionPlanDetailSerializer(NutritionPlanSerializer):
    meals = MealItemSerializer(many=True, read_only=True)

    class Meta(NutritionPlanSerializer.Meta):
        fields = NutritionPlanSerializer.Meta.fields + ['meals']
