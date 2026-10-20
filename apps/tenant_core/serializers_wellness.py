"""
apps/tenant_core/serializers_wellness.py — Serializers for Nutrition & Wellness MVP
"""

from decimal import Decimal
from rest_framework import serializers
from datetime import timedelta
from django.utils import timezone
from .models_wellness import (
    FoodItem,
    NutritionTarget,
    MealLog,
    MealLogItem,
    WaterLog,
    SleepLog,
    FoodCategory,
    FoodProvenance,
    MealType,
)



class FoodItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = FoodItem
        fields = [
            'id',
            'name',
            'regional_aliases',
            'category',
            'serving_unit',
            'serving_weight_grams',
            'household_measures',
            'calories',
            'protein_g',
            'carbs_g',
            'fat_g',
            'fiber_g',
            'provenance',
            'source_reference_id',
            'preparation_notes',
            'is_nutritionist_verified',
            'is_system',
        ]
        read_only_fields = fields


class MealLogItemSerializer(serializers.ModelSerializer):
    food_item_id = serializers.UUIDField(source='food_item.id', read_only=True)

    class Meta:
        model = MealLogItem
        fields = [
            'id',
            'food_item_id',
            'food_name_snapshot',
            'quantity',
            'unit_name',
            'consumed_grams',
            'snapshot_calories',
            'snapshot_protein_g',
            'snapshot_carbs_g',
            'snapshot_fat_g',
            'snapshot_fiber_g',
            'created_at',
        ]
        read_only_fields = fields


class MealLogItemInputSerializer(serializers.Serializer):
    food_item_id = serializers.UUIDField(required=True)
    quantity = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, default=Decimal('1.00'))
    unit_name = serializers.CharField(max_length=50, required=False, default='g')
    consumed_grams = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, allow_null=True)


class MealLogSerializer(serializers.ModelSerializer):
    items = MealLogItemSerializer(many=True, read_only=True)
    total_calories = serializers.SerializerMethodField()
    total_protein_g = serializers.SerializerMethodField()
    total_carbs_g = serializers.SerializerMethodField()
    total_fat_g = serializers.SerializerMethodField()
    total_fiber_g = serializers.SerializerMethodField()

    class Meta:
        model = MealLog
        fields = [
            'id',
            'logged_date',
            'logged_time',
            'meal_type',
            'notes',
            'total_calories',
            'total_protein_g',
            'total_carbs_g',
            'total_fat_g',
            'total_fiber_g',
            'items',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'total_calories',
            'total_protein_g',
            'total_carbs_g',
            'total_fat_g',
            'total_fiber_g',
            'items',
            'created_at',
            'updated_at',
        ]

    def get_total_calories(self, obj):
        return sum((item.snapshot_calories for item in obj.items.all()), Decimal('0.00'))

    def get_total_protein_g(self, obj):
        return sum((item.snapshot_protein_g for item in obj.items.all()), Decimal('0.00'))

    def get_total_carbs_g(self, obj):
        return sum((item.snapshot_carbs_g for item in obj.items.all()), Decimal('0.00'))

    def get_total_fat_g(self, obj):
        return sum((item.snapshot_fat_g for item in obj.items.all()), Decimal('0.00'))

    def get_total_fiber_g(self, obj):
        return sum((item.snapshot_fiber_g for item in obj.items.all()), Decimal('0.00'))


class CreateMealLogSerializer(serializers.Serializer):
    logged_date = serializers.DateField(required=True)
    logged_time = serializers.TimeField(required=False)
    meal_type = serializers.ChoiceField(choices=MealType.choices, required=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    items = MealLogItemInputSerializer(many=True, required=True)

    def validate_logged_date(self, value):
        if value > timezone.now().date() + timedelta(days=1):
            raise serializers.ValidationError("Cannot log meals for future dates.")
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one food item must be logged.")
        return value


class WaterLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = WaterLog
        fields = [
            'id',
            'logged_date',
            'amount_ml',
            'logged_at',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class CreateWaterLogSerializer(serializers.Serializer):
    logged_date = serializers.DateField(required=True)
    amount_ml = serializers.IntegerField(min_value=1, max_value=5000, required=True)
    logged_at = serializers.DateTimeField(required=False)

    def validate_logged_date(self, value):
        if value > timezone.now().date() + timedelta(days=1):
            raise serializers.ValidationError("Cannot log water for future dates.")
        return value


class SleepLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = SleepLog
        fields = [
            'id',
            'logged_date',
            'start_time',
            'end_time',
            'duration_minutes',
            'quality_rating',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CreateSleepLogSerializer(serializers.Serializer):
    logged_date = serializers.DateField(required=True)
    start_time = serializers.DateTimeField(required=True)
    end_time = serializers.DateTimeField(required=True)
    duration_minutes = serializers.IntegerField(min_value=1, max_value=1440, required=False)
    quality_rating = serializers.IntegerField(min_value=1, max_value=5, required=False, default=3)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_logged_date(self, value):
        if value > timezone.now().date() + timedelta(days=1):
            raise serializers.ValidationError("Cannot log sleep for future dates.")
        return value

    def validate(self, attrs):
        start = attrs.get('start_time')
        end = attrs.get('end_time')
        if start and end:
            if end <= start:
                raise serializers.ValidationError({"end_time": "end_time must be after start_time."})
            duration_sec = (end - start).total_seconds()
            if duration_sec > 86400:
                raise serializers.ValidationError({"end_time": "Sleep duration cannot exceed 24 hours (1440 minutes)."})
            if duration_sec < 60:
                raise serializers.ValidationError({"duration_minutes": "Sleep duration must be at least 1 minute."})
            calc_minutes = max(1, int(round(duration_sec / 60.0)))
            if not attrs.get('duration_minutes'):
                attrs['duration_minutes'] = calc_minutes
            elif attrs['duration_minutes'] > 1440:
                raise serializers.ValidationError({"duration_minutes": "duration_minutes cannot exceed 1440 minutes."})
        return attrs


class UpdateSleepLogSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField(required=False)
    end_time = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(min_value=1, max_value=1440, required=False)
    quality_rating = serializers.IntegerField(min_value=1, max_value=5, required=False)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        start = attrs.get('start_time')
        end = attrs.get('end_time')
        if start and end:
            if end <= start:
                raise serializers.ValidationError({"end_time": "end_time must be after start_time."})
            duration_sec = (end - start).total_seconds()
            if duration_sec > 86400:
                raise serializers.ValidationError({"end_time": "Sleep duration cannot exceed 24 hours (1440 minutes)."})
            if duration_sec < 60:
                raise serializers.ValidationError({"duration_minutes": "Sleep duration must be at least 1 minute."})
            if not attrs.get('duration_minutes'):
                attrs['duration_minutes'] = max(1, int(round(duration_sec / 60.0)))
        return attrs



class NutritionTargetSerializer(serializers.ModelSerializer):
    class Meta:
        model = NutritionTarget
        fields = [
            'id',
            'daily_calories',
            'protein_grams',
            'carbs_grams',
            'fat_grams',
            'water_ml',
            'sleep_target_minutes',
            'is_active',
            'updated_at',
        ]
        read_only_fields = ['id', 'updated_at']
