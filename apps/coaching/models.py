"""
Coaching Layer Models for PerformanceOS (Phase 2).
Provides Fitness Assessments, Body Metrics, Exercise Library, Workout Programs, and Nutrition Plans with Indian Food Dataset.
"""

from django.db import models
from django.utils import timezone
from apps.tenants.models import TenantAwareModel
from apps.users.models import User
from apps.members.models import Member

# ---------------------------------------------------------------------------
# 1. Fitness Assessments & Body Metrics
# ---------------------------------------------------------------------------

class AssessmentType(models.TextChoices):
    INITIAL = 'Initial', 'Initial Assessment'
    PERIODIC = 'Periodic', 'Periodic Check-in'
    MOBILITY = 'Mobility', 'Mobility & Posture'
    POST_PROGRAM = 'Post-Program', 'Post-Program Evaluation'


class FitnessAssessment(TenantAwareModel):
    """
    Physical and physiological assessment intake profile for a member.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Assessment ID (e.g. ASM-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='assessments'
    )
    assessed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='conducted_assessments'
    )
    assessment_date = models.DateField(default=timezone.localdate)

    assessment_type = models.CharField(
        max_length=32,
        choices=AssessmentType.choices,
        default=AssessmentType.INITIAL
    )

    # Anthropometric Body Data
    height_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    waist_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    hip_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    chest_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    arm_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    thigh_cm = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    bmi = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    body_fat_pct = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    muscle_mass_pct = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    # Strength & Mobility Baselines
    squat_1rm_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    bench_1rm_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    deadlift_1rm_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    plank_hold_sec = models.IntegerField(null=True, blank=True)

    mobility_score = models.IntegerField(default=70, help_text="Mobility / Flexibility score (0-100)")
    overall_fitness_score = models.IntegerField(default=75, help_text="Calculated overall score (0-100)")

    posture_notes = models.TextField(blank=True, default='')
    trainer_summary = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'coaching_assessments'
        ordering = ['-assessment_date']

    def __str__(self):
        return f"{self.member.name} - {self.assessment_type} ({self.assessment_date})"


# ---------------------------------------------------------------------------
# 2. Central Exercise Library
# ---------------------------------------------------------------------------

class MuscleGroup(models.TextChoices):
    CHEST = 'Chest', 'Chest'
    BACK = 'Back', 'Back'
    QUADRICEPS = 'Quadriceps', 'Quadriceps'
    HAMSTRINGS = 'Hamstrings', 'Hamstrings'
    GLUTES = 'Glutes', 'Glutes'
    SHOULDERS = 'Shoulders', 'Shoulders'
    ARMS = 'Arms', 'Arms'
    CORE = 'Core', 'Core'
    FULL_BODY = 'Full Body', 'Full Body'


class ExerciseCategory(models.TextChoices):
    STRENGTH = 'Strength', 'Strength Training'
    PILATES = 'Pilates', 'Reformer & Mat Pilates'
    CARDIO = 'Cardio', 'Cardiovascular'
    MOBILITY = 'Mobility', 'Mobility & Flexibility'
    CORE = 'Core', 'Core Stability'
    HIIT = 'HIIT', 'HIIT / Conditioning'


class DifficultyLevel(models.TextChoices):
    BEGINNER = 'Beginner', 'Beginner'
    INTERMEDIATE = 'Intermediate', 'Intermediate'
    ADVANCED = 'Advanced', 'Advanced'


class MovementPattern(models.TextChoices):
    SQUAT = 'Squat', 'Squat'
    HINGE = 'Hinge', 'Hinge'
    PUSH = 'Push', 'Push'
    PULL = 'Pull', 'Pull'
    LUNGE = 'Lunge', 'Lunge'
    CARRY = 'Carry', 'Carry'
    ROTATION = 'Rotation', 'Rotation'
    ISOMETRIC = 'Isometric', 'Isometric'


class Exercise(TenantAwareModel):
    """
    Central database of physical exercises with instruction, movement patterns, and coaching cues.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Exercise ID (e.g. EXE-001)"
    )
    name = models.CharField(max_length=255, help_text="e.g. Barbell Back Squat")
    category = models.CharField(
        max_length=32,
        choices=ExerciseCategory.choices,
        default=ExerciseCategory.STRENGTH
    )
    primary_muscle = models.CharField(
        max_length=32,
        choices=MuscleGroup.choices,
        default=MuscleGroup.FULL_BODY
    )
    equipment = models.CharField(max_length=64, default='Barbell')
    difficulty = models.CharField(
        max_length=32,
        choices=DifficultyLevel.choices,
        default=DifficultyLevel.BEGINNER
    )
    movement_pattern = models.CharField(
        max_length=32,
        choices=MovementPattern.choices,
        default=MovementPattern.SQUAT
    )
    instructions = models.TextField(help_text="Step-by-step technique breakdown")
    video_url = models.URLField(blank=True, default='')
    contraindications = models.TextField(blank=True, default='', help_text="Injuries or conditions to avoid this movement")

    class Meta:
        db_table = 'coaching_exercises'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.category} - {self.primary_muscle})"


# ---------------------------------------------------------------------------
# 3. Workout Programs & Split Builders
# ---------------------------------------------------------------------------

class WorkoutProgram(TenantAwareModel):
    """
    Trainer-designed multi-week workout program assigned to a member.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Program ID (e.g. PRG-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='workout_programs'
    )
    trainer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_workout_programs'
    )
    name = models.CharField(max_length=255, help_text="e.g. 12-Week Hypertrophy & Strength Program")
    goal = models.CharField(max_length=255, blank=True, default='')
    duration_weeks = models.IntegerField(default=12)
    days_per_week = models.IntegerField(default=4)
    start_date = models.DateField(default=timezone.localdate)

    end_date = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'coaching_workout_programs'
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.name} for {self.member.name}"


class WorkoutDay(TenantAwareModel):
    """
    Individual training day within a program (e.g. Day 1: Upper Body Push).
    """
    program = models.ForeignKey(
        WorkoutProgram,
        on_delete=models.CASCADE,
        related_name='workout_days'
    )
    day_name = models.CharField(max_length=128)
    day_order = models.IntegerField(default=1)

    class Meta:
        db_table = 'coaching_workout_days'
        ordering = ['day_order']

    def __str__(self):
        return f"{self.program.name} - {self.day_name}"


class WorkoutDayExercise(TenantAwareModel):
    """
    Specific exercise set prescribed inside a workout day.
    """
    workout_day = models.ForeignKey(
        WorkoutDay,
        on_delete=models.CASCADE,
        related_name='exercises'
    )
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.CASCADE
    )
    order = models.IntegerField(default=1)
    sets = models.IntegerField(default=3)
    reps = models.CharField(max_length=32, default='8-12')
    rest_seconds = models.IntegerField(default=60)
    tempo = models.CharField(max_length=32, blank=True, default='3-0-1-0')
    notes = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'coaching_workout_day_exercises'
        ordering = ['order']

    def __str__(self):
        return f"{self.exercise.name} ({self.sets} sets x {self.reps})"


# ---------------------------------------------------------------------------
# 4. Nutrition & Indian Food Dataset
# ---------------------------------------------------------------------------

class DietType(models.TextChoices):
    VEGETARIAN = 'Vegetarian', 'Vegetarian'
    NON_VEGETARIAN = 'Non-Vegetarian', 'Non-Vegetarian'
    VEGAN = 'Vegan', 'Vegan'
    JAIN = 'Jain', 'Jain'
    REGIONAL_INDIAN = 'Regional Indian', 'Regional Indian Diet'


class FoodItem(TenantAwareModel):
    """
    Comprehensive food catalog with macro breakdowns and Indian regional items.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Food ID (e.g. FOD-001)"
    )
    name = models.CharField(max_length=255, help_text="e.g. Paneer Tikka / Dal Tadka")
    diet_type = models.CharField(
        max_length=32,
        choices=DietType.choices,
        default=DietType.VEGETARIAN
    )
    serving_unit = models.CharField(max_length=64, default='100g')
    calories = models.DecimalField(max_digits=6, decimal_places=1)
    protein_g = models.DecimalField(max_digits=5, decimal_places=1)
    carbs_g = models.DecimalField(max_digits=5, decimal_places=1)
    fat_g = models.DecimalField(max_digits=5, decimal_places=1)
    fiber_g = models.DecimalField(max_digits=5, decimal_places=1, default=0.0)

    class Meta:
        db_table = 'coaching_food_items'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.calories} kcal / {self.protein_g}g P)"


class NutritionPlan(TenantAwareModel):
    """
    Personalized dietary plan prescribed by a trainer/nutritionist.
    """
    id = models.CharField(
        max_length=64,
        primary_key=True,
        help_text="Nutrition Plan ID (e.g. NUT-001)"
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name='nutrition_plans'
    )
    trainer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='assigned_nutrition_plans'
    )
    name = models.CharField(max_length=255, help_text="e.g. Lean Muscle & High-Protein Indian Diet")
    diet_type = models.CharField(
        max_length=32,
        choices=DietType.choices,
        default=DietType.VEGETARIAN
    )
    daily_calorie_target = models.IntegerField(default=2200)
    protein_target_g = models.IntegerField(default=150)
    carbs_target_g = models.IntegerField(default=220)
    fat_target_g = models.IntegerField(default=60)
    hydration_liters = models.DecimalField(max_digits=3, decimal_places=1, default=3.5)
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'coaching_nutrition_plans'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.member.name})"


class MealItem(TenantAwareModel):
    """
    Individual meal prescription in a nutrition plan.
    """
    plan = models.ForeignKey(
        NutritionPlan,
        on_delete=models.CASCADE,
        related_name='meals'
    )
    meal_time = models.CharField(
        max_length=64,
        choices=[
            ('Breakfast', 'Breakfast'),
            ('Mid-Morning', 'Mid-Morning Snack'),
            ('Lunch', 'Lunch'),
            ('Evening Snack', 'Evening Snack / Pre-Workout'),
            ('Dinner', 'Dinner'),
        ],
        default='Breakfast'
    )
    food_item = models.ForeignKey(
        FoodItem,
        on_delete=models.PROTECT
    )
    quantity = models.CharField(max_length=64, default='1 serving')
    calculated_calories = models.DecimalField(max_digits=6, decimal_places=1, default=0.0)

    class Meta:
        db_table = 'coaching_meal_items'
        ordering = ['id']

    def __str__(self):
        return f"{self.meal_time}: {self.food_item.name} ({self.quantity})"
