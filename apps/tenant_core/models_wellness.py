"""
apps/tenant_core/models_wellness.py — SWEAT Nutrition & Wellness MVP Domain Models

Tables:
1. FoodItem (food_items) — Local Indian food catalog & custom tenant foods.
2. NutritionTarget (nutrition_targets) — Daily member targets for calories, macros, water & sleep.
3. MealLog (meal_logs) — Daily meal logs (Breakfast, Lunch, Evening Snack, Dinner).
4. MealLogItem (meal_log_items) — Individual food items logged with immutable nutrient snapshots.
5. WaterLog (water_logs) — Intraday water consumption tracking.
6. SleepLog (sleep_logs) — Manual sleep tracking with duration and quality ratings.

All models belong strictly to the tenant database schema (app_label='tenant_core').
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization
from .models_users import TenantUser
from .models_workforce import UserProfile


class FoodCategory(models.TextChoices):
    GRAINS = 'GRAINS', 'Grains & Cereals'
    PULSES_LEGUMES = 'PULSES_LEGUMES', 'Pulses, Lentils & Legumes'
    DAIRY = 'DAIRY', 'Dairy & Eggs'
    MEAT_POULTRY = 'MEAT_POULTRY', 'Meat & Poultry'
    VEGETABLES = 'VEGETABLES', 'Vegetables'
    FRUITS = 'FRUITS', 'Fruits'
    OILS_FATS = 'OILS_FATS', 'Oils, Ghee & Fats'
    NUTS_SEEDS = 'NUTS_SEEDS', 'Nuts & Seeds'
    SNACKS_SWEETS = 'SNACKS_SWEETS', 'Snacks & Sweets'
    BEVERAGES = 'BEVERAGES', 'Beverages'
    OTHER = 'OTHER', 'Other'


class FoodProvenance(models.TextChoices):
    USDA_DIRECT = 'USDA_DIRECT', 'USDA Direct'
    RECIPE_ESTIMATE = 'RECIPE_ESTIMATE', 'Recipe Estimate'
    CUSTOM_TENANT = 'CUSTOM_TENANT', 'Custom Tenant Food'


class MealType(models.TextChoices):
    BREAKFAST = 'BREAKFAST', 'Breakfast'
    LUNCH = 'LUNCH', 'Lunch'
    EVENING_SNACK = 'EVENING_SNACK', 'Evening Snack'
    DINNER = 'DINNER', 'Dinner'


class FoodItem(models.Model):
    """
    Catalog of food items. Can be system-wide (is_system=True, organization=None)
    or custom foods created by a specific organization (is_system=False, organization set).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='food_items',
        help_text='Null for global platform catalog items; set for tenant-specific custom foods.'
    )
    is_system = models.BooleanField(default=True, db_index=True)
    name = models.CharField(max_length=200, db_index=True)
    regional_aliases = models.JSONField(
        default=list,
        blank=True,
        help_text='List of alternate or regional names, e.g. ["Roti", "Chapati", "Phulka"]'
    )
    category = models.CharField(
        max_length=50,
        choices=FoodCategory.choices,
        default=FoodCategory.OTHER
    )
    serving_unit = models.CharField(max_length=50, default='g', help_text='Base serving unit, e.g. g, piece, cup, ml')
    serving_weight_grams = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=Decimal('100.00'),
        help_text='Net weight in grams corresponding to the standard serving values below.'
    )
    household_measures = models.JSONField(
        default=list,
        blank=True,
        help_text='Alternative portion selections, e.g. [{"name": "1 medium phulka", "weight_g": 35.0, "unit": "piece"}]'
    )
    # Nutrients for standard serving (serving_weight_grams)
    calories = models.DecimalField(max_digits=7, decimal_places=2, help_text='Energy in kcal')
    protein_g = models.DecimalField(max_digits=6, decimal_places=2, help_text='Protein in grams')
    carbs_g = models.DecimalField(max_digits=6, decimal_places=2, help_text='Carbohydrates in grams')
    fat_g = models.DecimalField(max_digits=6, decimal_places=2, help_text='Total fat in grams')
    fiber_g = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text='Dietary fiber in grams')

    provenance = models.CharField(
        max_length=30,
        choices=FoodProvenance.choices,
        default=FoodProvenance.RECIPE_ESTIMATE
    )
    source_reference_id = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        help_text='Source identifier such as USDA FDC ID or internal recipe reference'
    )
    preparation_notes = models.TextField(
        blank=True,
        default='',
        help_text='Documented recipe composition, raw ingredients, cooking oil, and yield assumptions'
    )
    is_nutritionist_verified = models.BooleanField(
        default=False,
        help_text='Must remain False unless formally verified by a certified nutritionist'
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'food_items'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.provenance}) - {self.calories} kcal"


class NutritionTarget(models.Model):
    """
    Daily nutrition, water, and sleep goals configured for a member profile.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='nutrition_targets')
    user = models.ForeignKey(TenantUser, on_delete=models.CASCADE, related_name='nutrition_targets')
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='nutrition_targets')
    daily_calories = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('2000.00'))
    protein_grams = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('120.00'))
    carbs_grams = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('220.00'))
    fat_grams = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('65.00'))
    water_ml = models.PositiveIntegerField(default=2500, help_text='Daily water intake goal in millilitres')
    sleep_target_minutes = models.PositiveIntegerField(default=480, help_text='Daily sleep goal in minutes (default 8h = 480m)')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'nutrition_targets'
        ordering = ['-created_at']

    def __str__(self):
        return f"Targets for {self.user.email}: {self.daily_calories} kcal / {self.protein_grams}g P"


class MealLog(models.Model):
    """
    Header record representing a meal logged on a particular calendar date.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='meal_logs')
    user = models.ForeignKey(TenantUser, on_delete=models.CASCADE, related_name='meal_logs')
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='meal_logs')
    logged_date = models.DateField(db_index=True)
    logged_time = models.TimeField(default=timezone.now)
    meal_type = models.CharField(max_length=30, choices=MealType.choices)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'meal_logs'
        ordering = ['-logged_date', '-logged_time']

    def __str__(self):
        return f"{self.user.email} - {self.meal_type} on {self.logged_date}"


class MealLogItem(models.Model):
    """
    Line item for a logged meal. Captures an immutable snapshot of calculated
    nutrients so historical logs never change if catalog values are updated.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meal_log = models.ForeignKey(MealLog, on_delete=models.CASCADE, related_name='items')
    food_item = models.ForeignKey(FoodItem, on_delete=models.PROTECT, related_name='logged_instances')
    food_name_snapshot = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('1.00'))
    unit_name = models.CharField(max_length=50, default='g')
    consumed_grams = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        help_text='Calculated net weight of food consumed in grams'
    )
    # Immutable snapshots at the time of logging
    snapshot_calories = models.DecimalField(max_digits=7, decimal_places=2)
    snapshot_protein_g = models.DecimalField(max_digits=6, decimal_places=2)
    snapshot_carbs_g = models.DecimalField(max_digits=6, decimal_places=2)
    snapshot_fat_g = models.DecimalField(max_digits=6, decimal_places=2)
    snapshot_fiber_g = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'meal_log_items'
        ordering = ['created_at']

    def __str__(self):
        return f"{self.food_name_snapshot} ({self.quantity} {self.unit_name}) - {self.snapshot_calories} kcal"


class WaterLog(models.Model):
    """
    Intraday water consumption record.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='water_logs')
    user = models.ForeignKey(TenantUser, on_delete=models.CASCADE, related_name='water_logs')
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='water_logs')
    logged_date = models.DateField(db_index=True)
    amount_ml = models.PositiveIntegerField(help_text='Amount of water consumed in millilitres')
    logged_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'water_logs'
        ordering = ['-logged_date', '-logged_at']

    def __str__(self):
        return f"{self.user.email} - {self.amount_ml}ml on {self.logged_date}"


class SleepLog(models.Model):
    """
    Manual sleep entry with duration and subjective quality rating.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sleep_logs')
    user = models.ForeignKey(TenantUser, on_delete=models.CASCADE, related_name='sleep_logs')
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='sleep_logs')
    logged_date = models.DateField(db_index=True, help_text='Date the sleep corresponds to')
    start_time = models.DateTimeField(help_text='Timestamp of sleep onset')
    end_time = models.DateTimeField(help_text='Timestamp of wake-up')
    duration_minutes = models.PositiveIntegerField(help_text='Calculated sleep duration in minutes')
    quality_rating = models.PositiveSmallIntegerField(
        default=3,
        help_text='Subjective rating from 1 (poor) to 5 (excellent)'
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'sleep_logs'
        ordering = ['-logged_date']

    def __str__(self):
        return f"{self.user.email} - {self.duration_minutes}m sleep ({self.logged_date})"
