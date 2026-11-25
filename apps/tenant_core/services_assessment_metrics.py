"""
services_assessment_metrics.py — Metabolic & Biometric Calculations for Functional Assessment Module.

Implements:
1. Real member attributes (DOB/Age and biological sex resolution from UserProfile).
2. Mifflin-St Jeor RMR/BMR & TDEE formulas with mandatory scientific disclaimers.
3. BMI calculation, classification, and screening disclaimers.
4. Goal-directed calorie and macronutrient distributions.
5. Strict omission of fake computer-vision stubs and stock milestone images (Unsplash).
"""

from datetime import date
from decimal import Decimal
from typing import Dict, Any, Optional
from .models_crm import UserProfile

BMI_DISCLAIMER = "BMI is a screening metric, not a direct measure of body fat or muscularity."
RMR_DISCLAIMER = "Estimated via Mifflin-St Jeor formula with physical activity multiplier. Individual metabolic rates vary."

ACTIVITY_MULTIPLIERS = {
    'SEDENTARY': 1.20,
    'LIGHTLY_ACTIVE': 1.375,
    'MODERATELY_ACTIVE': 1.55,
    'HIGHLY_ACTIVE': 1.725,
}


def resolve_member_demographics(
    user_profile: Optional[UserProfile] = None,
    explicit_age: Optional[int] = None,
    explicit_sex: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolves member's true age and biological sex from profile with provenance awareness.
    """
    resolved_age: Optional[int] = None
    age_provenance = 'UNAVAILABLE'

    dob = (getattr(user_profile, 'date_of_birth', None) or getattr(user_profile, 'dob', None)) if user_profile else None
    if explicit_age is not None and explicit_age > 0:
        resolved_age = int(explicit_age)
        age_provenance = 'MEMBER_REPORTED'
    elif dob:
        today = date.today()
        resolved_age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        age_provenance = 'CALCULATED'

    resolved_sex: str = 'FEMALE'  # fallback standard baseline if unknown
    sex_provenance = 'UNAVAILABLE'

    if explicit_sex and str(explicit_sex).upper() in ['MALE', 'M', 'FEMALE', 'F']:
        resolved_sex = 'MALE' if str(explicit_sex).upper() in ['MALE', 'M'] else 'FEMALE'
        sex_provenance = 'MEMBER_REPORTED'
    elif user_profile and getattr(user_profile, 'gender', None):
        g = str(user_profile.gender).upper()
        if g in ['MALE', 'M']:
            resolved_sex = 'MALE'
            sex_provenance = 'MEASURED'
        elif g in ['FEMALE', 'F']:
            resolved_sex = 'FEMALE'
            sex_provenance = 'MEASURED'

    # Fallback age if completely unrecorded
    if resolved_age is None or resolved_age <= 0:
        resolved_age = 30
        age_provenance = 'ESTIMATED'

    return {
        'age': resolved_age,
        'age_provenance': age_provenance,
        'sex': resolved_sex,
        'sex_provenance': sex_provenance,
    }


def calculate_bmi(weight_kg: float, height_cm: float) -> Dict[str, Any]:
    """
    Computes BMI and WHO classification with mandatory screening disclaimer.
    """
    w = float(weight_kg)
    h_m = float(height_cm) / 100.0
    if h_m <= 0:
        return {'bmi': None, 'category': 'Unknown', 'disclaimer': BMI_DISCLAIMER}

    bmi_val = round(w / (h_m * h_m), 1)

    if bmi_val < 18.5:
        category = 'Underweight'
    elif bmi_val < 25.0:
        category = 'Normal weight'
    elif bmi_val < 30.0:
        category = 'Overweight'
    elif bmi_val < 35.0:
        category = 'Obesity Class I'
    elif bmi_val < 40.0:
        category = 'Obesity Class II'
    else:
        category = 'Obesity Class III'

    return {
        'bmi': bmi_val,
        'category': category,
        'disclaimer': BMI_DISCLAIMER,
        'height_m': round(h_m, 2),
        'weight_kg': round(w, 1),
    }


def calculate_metabolic_nutrition_plan(
    weight_kg: float,
    height_cm: float,
    user_profile: Optional[UserProfile] = None,
    age: Optional[int] = None,
    sex: Optional[str] = None,
    activity_level: str = 'MODERATELY_ACTIVE',
    goal: str = 'POSTURE_ALIGNMENT',
) -> Dict[str, Any]:
    """
    Calculates RMR (Mifflin-St Jeor), TDEE, Calorie Target, and Macro Targets
    using real member demographics and providing explicit scientific disclaimers.
    """
    demo = resolve_member_demographics(user_profile, age, sex)
    w = float(weight_kg)
    h = float(height_cm)
    a = int(demo['age'])
    resolved_sex = demo['sex']

    # 1. RMR / BMR via Mifflin-St Jeor equation
    # Male:   10 * W + 6.25 * H - 5 * A + 5
    # Female: 10 * W + 6.25 * H - 5 * A - 161
    if resolved_sex == 'MALE':
        rmr_raw = (10.0 * w) + (6.25 * h) - (5.0 * a) + 5.0
    else:
        rmr_raw = (10.0 * w) + (6.25 * h) - (5.0 * a) - 161.0

    rmr_val = int(round(rmr_raw))

    # 2. Activity Multiplier for TDEE
    mult = ACTIVITY_MULTIPLIERS.get(str(activity_level).upper(), 1.55)
    tdee_val = int(round(rmr_val * mult))

    # 3. Goal-directed Daily Calorie Target
    goal_norm = str(goal).upper()
    if any(k in goal_norm for k in ['FAT_LOSS', 'WEIGHT_LOSS', 'LEAN']):
        calorie_target = max(1200, tdee_val - 400)
        goal_strategy = "Moderate 400 kcal deficit targeting sustainable fat loss while preserving lean mass."
    elif any(k in goal_norm for k in ['HYPERTROPHY', 'MUSCLE', 'STRENGTH', 'PERFORMANCE']):
        calorie_target = tdee_val + 250
        goal_strategy = "Controlled 250 kcal surplus to fuel progressive overload and muscle synthesis."
    else:
        calorie_target = tdee_val
        goal_strategy = "Maintenance calories to support structural alignment, recovery, and neuromuscular adaptations."

    # 4. Macronutrient Targets
    # Protein: 1.8g per kg bodyweight
    protein_g = int(round(w * 1.8))
    protein_kcal = protein_g * 4

    # Fat: 25% of total calories (9 kcal/g)
    fat_kcal = calorie_target * 0.25
    fat_g = int(round(fat_kcal / 9.0))

    # Carbohydrates: remaining calories (4 kcal/g)
    carb_kcal = max(0, calorie_target - protein_kcal - int(fat_kcal))
    carb_g = int(round(carb_kcal / 4.0))

    # Hydration: ~35-40 ml per kg
    hydration_l = round(w * 0.038, 1)

    bmi_info = calculate_bmi(w, h)

    return {
        'rmr_kcal': rmr_val,
        'tdee_kcal': tdee_val,
        'daily_calorie_target': calorie_target,
        'goal_strategy': goal_strategy,
        'activity_multiplier': mult,
        'demographics_used': {
            'age': a,
            'age_provenance': demo['age_provenance'],
            'sex': resolved_sex,
            'sex_provenance': demo['sex_provenance'],
        },
        'disclaimers': {
            'rmr': RMR_DISCLAIMER,
            'bmi': BMI_DISCLAIMER,
        },
        'bmi': bmi_info['bmi'],
        'bmi_category': bmi_info['category'],
        'macros': {
            'protein_g': protein_g,
            'carbs_g': carb_g,
            'fat_g': fat_g,
            'fiber_g': 28,
            'hydration_liters': hydration_l,
            'meal_guidance': (
                f"Distribute {protein_g}g daily protein across 3-4 meals. "
                f"Drink ~{hydration_l}L water daily for optimal connective tissue hydration."
            ),
        },
    }
