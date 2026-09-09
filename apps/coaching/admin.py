from django.contrib import admin
from django.utils.html import format_html
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


class WorkoutDayExerciseInline(admin.TabularInline):
    model = WorkoutDayExercise
    extra = 0
    fields = ('exercise', 'order', 'target_sets', 'target_reps', 'rest_seconds')


class WorkoutDayInline(admin.StackedInline):
    model = WorkoutDay
    extra = 0
    fields = ('day_number', 'title', 'focus_area')


class MealItemInline(admin.TabularInline):
    model = MealItem
    extra = 0
    fields = ('meal_type', 'food_item', 'quantity', 'calories', 'protein_g')


@admin.register(FitnessAssessment)
class FitnessAssessmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'member', 'type_badge', 'assessment_date', 'weight_display', 'bmi_display', 'score_display', 'tenant')
    list_filter = ('assessment_type', 'assessment_date', 'tenant')
    search_fields = ('member__name', 'trainer_summary', 'tenant__name')

    def type_badge(self, obj):
        return format_html(f'<span style="background: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.assessment_type}</span>')
    type_badge.short_description = 'Type'

    def weight_display(self, obj):
        return f"{obj.weight_kg} kg"
    weight_display.short_description = 'Weight'

    def bmi_display(self, obj):
        return f"{obj.bmi:.1f}"
    bmi_display.short_description = 'BMI'

    def score_display(self, obj):
        return format_html(f'<b style="color: #059669;">{obj.overall_fitness_score}/100</b>')
    score_display.short_description = 'Fitness Score'


@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category_badge', 'muscle_badge', 'diff_badge', 'equipment', 'tenant')
    list_filter = ('category', 'primary_muscle', 'difficulty', 'tenant')
    search_fields = ('name', 'instructions', 'tenant__name')

    def category_badge(self, obj):
        return format_html(f'<span style="background: #f1f5f9; color: #334155; padding: 2px 6px; border-radius: 4px; font-size: 11px;">{obj.category}</span>')
    category_badge.short_description = 'Category'

    def muscle_badge(self, obj):
        return format_html(f'<span style="background: #dbeafe; color: #1e40af; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.primary_muscle}</span>')
    muscle_badge.short_description = 'Muscle'

    def diff_badge(self, obj):
        colors = {
            'Beginner': 'color: #059669;',
            'Intermediate': 'color: #d97706;',
            'Advanced': 'color: #dc2626;',
        }
        style = colors.get(obj.difficulty, 'color: #4b5563;')
        return format_html(f'<span style="{style} font-weight: bold;">{obj.difficulty}</span>')
    diff_badge.short_description = 'Difficulty'


@admin.register(WorkoutProgram)
class WorkoutProgramAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'member', 'trainer', 'duration_display', 'frequency_display', 'status_badge', 'tenant')
    list_filter = ('is_active', 'tenant')
    search_fields = ('name', 'member__name', 'tenant__name')
    inlines = [WorkoutDayInline]

    def duration_display(self, obj):
        return f"{obj.duration_weeks} Weeks"
    duration_display.short_description = 'Duration'

    def frequency_display(self, obj):
        return f"{obj.days_per_week} Days/Wk"
    frequency_display.short_description = 'Frequency'

    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #059669; font-weight: bold;">ACTIVE</span>')
        return format_html('<span style="color: #dc2626; font-weight: bold;">INACTIVE</span>')
    status_badge.short_description = 'Status'


@admin.register(FoodItem)
class FoodItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'diet_badge', 'calories_display', 'macros_display', 'tenant')
    list_filter = ('diet_type', 'tenant')
    search_fields = ('name', 'tenant__name')

    def diet_badge(self, obj):
        return format_html(f'<span style="background: #d1fae5; color: #065f46; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.diet_type}</span>')
    diet_badge.short_description = 'Diet'

    def calories_display(self, obj):
        return f"{obj.calories} kcal / {obj.serving_unit}"
    calories_display.short_description = 'Serving'

    def macros_display(self, obj):
        return format_html(f'<span style="font-size: 11px;">P: <b>{obj.protein_g}g</b> | C: <b>{obj.carbs_g}g</b> | F: <b>{obj.fat_g}g</b></span>')
    macros_display.short_description = 'Macros'


@admin.register(NutritionPlan)
class NutritionPlanAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'member', 'diet_badge', 'calorie_target_display', 'status_badge', 'tenant')
    list_filter = ('diet_type', 'is_active', 'tenant')
    search_fields = ('name', 'member__name', 'tenant__name')
    inlines = [MealItemInline]

    def diet_badge(self, obj):
        return format_html(f'<span style="background: #d1fae5; color: #065f46; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;">{obj.diet_type}</span>')
    diet_badge.short_description = 'Diet'

    def calorie_target_display(self, obj):
        return f"{obj.daily_calorie_target} kcal / day"
    calorie_target_display.short_description = 'Daily Target'

    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #059669; font-weight: bold;">ACTIVE</span>')
        return format_html('<span style="color: #dc2626; font-weight: bold;">INACTIVE</span>')
    status_badge.short_description = 'Status'
