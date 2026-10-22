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
            'barcode',
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
    food_item_id = serializers.UUIDField(required=False, allow_null=True)
    custom_food_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    quantity = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, default=Decimal('1.00'))
    unit_name = serializers.CharField(max_length=50, required=False, default='g')
    consumed_grams = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, allow_null=True)
    portion_grams = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, allow_null=True)
    calories = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, allow_null=True)
    calories_per_100g = serializers.DecimalField(max_digits=7, decimal_places=2, required=False, allow_null=True)
    protein_g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    protein_per_100g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    carbs_g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    carbs_per_100g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    fat_g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    fat_per_100g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    fiber_g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)
    fiber_per_100g = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True)

    def validate(self, attrs):
        food_id = attrs.get('food_item_id')
        custom_name = (attrs.get('custom_food_name') or '').strip()
        if not food_id and not custom_name:
            raise serializers.ValidationError("Either food_item_id or custom_food_name must be provided.")

        if not food_id and custom_name:
            grams = attrs.get('consumed_grams') or attrs.get('portion_grams')
            if grams is not None and grams <= 0:
                raise serializers.ValidationError({"portion_grams": "Portion grams must be greater than zero."})

            base_cal = attrs.get('calories_per_100g') if attrs.get('calories_per_100g') is not None else attrs.get('calories')
            if base_cal is None:
                raise serializers.ValidationError({"calories_per_100g": "Base calories per 100g must be provided for custom food items."})
            if base_cal < 0:
                raise serializers.ValidationError({"calories_per_100g": "Base calories cannot be negative."})

            for field in ('protein_g', 'protein_per_100g', 'carbs_g', 'carbs_per_100g', 'fat_g', 'fat_per_100g', 'fiber_g', 'fiber_per_100g'):
                val = attrs.get(field)
                if val is not None and val < 0:
                    raise serializers.ValidationError({field: f"{field} cannot be negative."})

        return attrs


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
    logged_date = serializers.DateField(required=False)
    date = serializers.DateField(required=False)
    logged_time = serializers.TimeField(required=False)
    meal_type = serializers.ChoiceField(choices=MealType.choices, required=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    items = MealLogItemInputSerializer(many=True, required=True)

    def validate(self, attrs):
        target_date = attrs.get('logged_date') or attrs.get('date') or timezone.now().date()
        if target_date > timezone.now().date() + timedelta(days=1):
            raise serializers.ValidationError("Cannot log meals for future dates.")
        attrs['logged_date'] = target_date
        return attrs

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
    logged_date = serializers.DateField(required=False)
    date = serializers.DateField(required=False)
    amount_ml = serializers.IntegerField(min_value=1, max_value=5000, required=True)
    logged_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        target_date = attrs.get('logged_date') or attrs.get('date') or timezone.now().date()
        if target_date > timezone.now().date() + timedelta(days=1):
            raise serializers.ValidationError({"logged_date": "Cannot log water for future dates."})
        attrs['logged_date'] = target_date
        return attrs


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
