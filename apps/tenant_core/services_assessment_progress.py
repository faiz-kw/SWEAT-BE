"""
services_assessment_progress.py — Like-for-Like Reassessment Comparison & Test Progression Engine.

Implements:
1. Like-for-like comparison between current assessment and baseline/previous assessment.
2. Non-equivalent protocol detection and explanatory warnings (e.g., knee push-up vs floor push-up).
3. Vitals and capability matrix progress deltas.
4. Coach advancement approval and progression tracking for AssessmentTestProgression.
"""

from decimal import Decimal
from typing import Dict, Any, Optional, List
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_assessment import MemberAssessment, AssessmentTestProgression
from .models_users import TenantUser
from .services_reliability import record_business_audit


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == '':
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def compare_assessments(
    current: MemberAssessment,
    previous: Optional[MemberAssessment] = None,
) -> Dict[str, Any]:
    """
    Performs longitudinal like-for-like comparison between two assessments.
    Detects protocol discrepancies, computes quantitative deltas, and tracks quality progression.
    """
    if previous is None:
        previous = current.previous_assessment

    if not previous:
        return {
            'has_baseline': False,
            'message': 'No previous assessment linked for comparison.',
            'vitals_comparison': {},
            'movement_comparisons': [],
            'capability_comparison': {},
        }

    # 1. Vitals Comparison
    curr_wt = _safe_float(current.measured_weight_kg)
    prev_wt = _safe_float(previous.measured_weight_kg)
    curr_bmi = _safe_float(current.calculated_bmi)
    prev_bmi = _safe_float(previous.calculated_bmi)
    curr_waist = _safe_float(current.waist_circumference_cm)
    prev_waist = _safe_float(previous.waist_circumference_cm)
    curr_hr = current.resting_heart_rate_bpm
    prev_hr = previous.resting_heart_rate_bpm

    vitals_diff = {
        'weight_kg': {
            'previous': prev_wt,
            'current': curr_wt,
            'delta': round(curr_wt - prev_wt, 2) if (curr_wt is not None and prev_wt is not None) else None,
        },
        'bmi': {
            'previous': prev_bmi,
            'current': curr_bmi,
            'delta': round(curr_bmi - prev_bmi, 2) if (curr_bmi is not None and prev_bmi is not None) else None,
        },
        'waist_cm': {
            'previous': prev_waist,
            'current': curr_waist,
            'delta': round(curr_waist - prev_waist, 2) if (curr_waist is not None and prev_waist is not None) else None,
        },
        'resting_heart_rate_bpm': {
            'previous': prev_hr,
            'current': curr_hr,
            'delta': (curr_hr - prev_hr) if (curr_hr is not None and prev_hr is not None) else None,
        },
    }

    # 2. Movement Battery Comparison (Domain by Domain)
    curr_tests = current.functional_tests_payload or {}
    prev_tests = previous.functional_tests_payload or {}
    all_domains = set(list(curr_tests.keys()) + list(prev_tests.keys()))

    movement_comparisons: List[Dict[str, Any]] = []

    for domain in sorted(all_domains):
        curr_domain_data = curr_tests.get(domain, {})
        prev_domain_data = prev_tests.get(domain, {})

        # Normalize tests to list of test objects
        def _extract_tests(data: Any) -> List[Dict[str, Any]]:
            if isinstance(data, dict):
                if isinstance(data.get('assessed_tests'), list):
                    return [t for t in data['assessed_tests'] if isinstance(t, dict)]
                return [data]
            return []

        curr_items = _extract_tests(curr_domain_data)
        prev_items = _extract_tests(prev_domain_data)

        # Pair items by name or test index
        matched_pairs: List[tuple] = []
        for c_item in curr_items:
            c_name = str(c_item.get('name') or c_item.get('test_name') or domain).lower()
            match = None
            for p_item in prev_items:
                p_name = str(p_item.get('name') or p_item.get('test_name') or domain).lower()
                if c_name == p_name or (c_name in p_name) or (p_name in c_name):
                    match = p_item
                    break
            matched_pairs.append((c_item, match))

        for c_item, p_item in matched_pairs:
            test_title = c_item.get('name') or c_item.get('test_name') or domain.replace('_', ' ').title()
            curr_var = str(c_item.get('variation') or c_item.get('pushup_variation') or 'Standard').strip()
            prev_var = str(p_item.get('variation') or p_item.get('pushup_variation') or 'Standard').strip() if p_item else None

            is_non_equivalent = False
            comparison_note = ''
            if p_item and curr_var.lower() != prev_var.lower():
                is_non_equivalent = True
                comparison_note = (
                    f"Non-equivalent protocol: Exercise variation changed from '{prev_var}' to '{curr_var}'. "
                    f"Direct rep/metric comparison is not clinically valid."
                )

            curr_reps = _safe_float(c_item.get('reps') or c_item.get('pushup_reps'))
            prev_reps = _safe_float(p_item.get('reps') or p_item.get('pushup_reps')) if p_item else None
            reps_delta = (curr_reps - prev_reps) if (curr_reps is not None and prev_reps is not None and not is_non_equivalent) else None

            curr_load = _safe_float(c_item.get('load_kg'))
            prev_load = _safe_float(p_item.get('load_kg')) if p_item else None
            load_delta = (curr_load - prev_load) if (curr_load is not None and prev_load is not None and not is_non_equivalent) else None

            movement_comparisons.append({
                'domain': domain,
                'test_title': test_title,
                'previous_variation': prev_var,
                'current_variation': curr_var,
                'non_equivalent_comparison': is_non_equivalent,
                'comparison_note': comparison_note,
                'previous_reps': prev_reps,
                'current_reps': curr_reps,
                'reps_delta': reps_delta,
                'previous_load_kg': prev_load,
                'current_load_kg': curr_load,
                'load_delta': load_delta,
                'previous_quality': p_item.get('movement_quality') if p_item else None,
                'current_quality': c_item.get('movement_quality'),
            })

    # 3. Capability Matrix Progress
    curr_matrix = current.capability_matrix or {}
    prev_matrix = previous.capability_matrix or {}
    matrix_comp = {}
    for dom in sorted(set(list(curr_matrix.keys()) + list(prev_matrix.keys()))):
        matrix_comp[dom] = {
            'previous': prev_matrix.get(dom, 'Not Assessed'),
            'current': curr_matrix.get(dom, 'Not Assessed'),
        }

    return {
        'has_baseline': True,
        'current_assessment_id': str(current.id),
        'previous_assessment_id': str(previous.id),
        'previous_date': previous.created_at.date().isoformat() if previous.created_at else None,
        'current_date': current.created_at.date().isoformat() if current.created_at else None,
        'vitals_comparison': vitals_diff,
        'movement_comparisons': movement_comparisons,
        'capability_comparison': matrix_comp,
    }


@transaction.atomic
def approve_progression(
    progression: AssessmentTestProgression,
    coach_user: TenantUser,
    criteria_met_notes: str,
    next_variation: str,
    db_alias: str = 'default',
) -> AssessmentTestProgression:
    """
    Coach signs off on a test progression advancement.
    Enforces that advancement cannot happen automatically without coach approval.
    """
    user_type = getattr(coach_user, 'user_type', None)
    is_staff = getattr(coach_user, 'is_staff', False)
    if user_type == 'MEMBER' and not is_staff:
        raise ValidationError("Members cannot approve their own movement test progressions.")

    progression.coach_approved_by = coach_user
    progression.movement_quality = 'Meets criteria'
    if criteria_met_notes:
        progression.progression_criteria = criteria_met_notes.strip()
    if next_variation:
        progression.next_progression_target = next_variation.strip()

    progression.save(using=db_alias)

    record_business_audit(
        organization=progression.organization,
        module='core',
        action_code='ASSESSMENT_PROGRESSION_APPROVED',
        entity_type='AssessmentTestProgression',
        entity_id=progression.id,
        actor_user=coach_user,
        event_description=f"Coach {coach_user.first_name} approved progression for {progression.exercise_name}",
        after_data={
            'exercise_name': progression.exercise_name,
            'current_variation': progression.current_variation,
            'next_progression_target': progression.next_progression_target,
        },
        db_alias=db_alias,
    )

    return progression
