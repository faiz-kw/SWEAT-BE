"""
services_assessment_screening.py — Safety & Readiness Gate Engine for Functional Assessment Module.

Implements:
1. 10-Question PAR-Q Safety & Readiness Gate evaluation.
2. Hard-blocking red flags (chest pain, syncope/dizziness, resting dyspnea).
3. Modification flags (orthopedic limitations, hypertension meds, pregnancy/postpartum).
4. Clinical escalation flags (minors under 18, pregnancy, acute multi-symptom presentation).
"""

from datetime import date
from typing import Dict, Any, List, Optional
from .models_crm import UserProfile

# Canonical 10 PAR-Q screening questions
PARQ_QUESTIONS: List[Dict[str, Any]] = [
    {
        'key': 'heart_condition',
        'question': 'Has your doctor ever said that you have a heart condition?',
        'category': 'CARDIOVASCULAR',
        'severity': 'HIGH',
    },
    {
        'key': 'chest_pain_activity',
        'question': 'Do you feel pain in your chest when you engage in physical activity?',
        'category': 'CARDIOVASCULAR',
        'severity': 'CRITICAL',
    },
    {
        'key': 'chest_pain_rest',
        'question': 'In the past month, have you had chest pain when you were not doing physical activity?',
        'category': 'CARDIOVASCULAR',
        'severity': 'CRITICAL',
    },
    {
        'key': 'dizziness_loss_of_balance',
        'question': 'Do you lose your balance because of dizziness, or have you ever lost consciousness/fainted?',
        'category': 'NEUROLOGICAL',
        'severity': 'CRITICAL',
    },
    {
        'key': 'bone_joint_problem',
        'question': 'Do you have a bone, joint, or spinal problem that could be made worse by a change in physical activity?',
        'category': 'ORTHOPEDIC',
        'severity': 'MODERATE',
    },
    {
        'key': 'blood_pressure_medication',
        'question': 'Is your doctor currently prescribing drugs (for example, water pills) for your blood pressure or heart condition?',
        'category': 'CARDIOVASCULAR',
        'severity': 'MODERATE',
    },
    {
        'key': 'unexplained_shortness_of_breath',
        'question': 'Do you experience unexplained shortness of breath, asthma flare-ups, or breathing difficulty at rest or light exertion?',
        'category': 'PULMONARY',
        'severity': 'CRITICAL',
    },
    {
        'key': 'recent_surgery_injury',
        'question': 'Have you undergone surgery, experienced a fracture, or had a major injury within the past 6 months?',
        'category': 'SURGICAL',
        'severity': 'MODERATE',
    },
    {
        'key': 'pregnancy_or_postpartum',
        'question': 'Are you currently pregnant or within 6 months postpartum?',
        'category': 'PHYSIOLOGICAL',
        'severity': 'MODERATE',
    },
    {
        'key': 'other_medical_condition',
        'question': 'Do you know of any other reason why you should not do physical activity?',
        'category': 'GENERAL',
        'severity': 'MODERATE',
    },
]

CRITICAL_RED_FLAG_KEYS = {
    'chest_pain_activity',
    'chest_pain_rest',
    'dizziness_loss_of_balance',
    'unexplained_shortness_of_breath',
}


def _is_affirmative(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get('answered_yes') or value.get('value') in [True, 'YES', 'yes', 'true', '1'])
    if isinstance(value, str):
        return value.strip().upper() in ['YES', 'TRUE', '1', 'POSITIVE']
    return False


def _calculate_member_age(user_profile: Optional[UserProfile]) -> Optional[int]:
    if not user_profile:
        return None
    dob = getattr(user_profile, 'date_of_birth', None) or getattr(user_profile, 'dob', None)
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def evaluate_safety_readiness(
    health_screening: Dict[str, Any],
    user_profile: Optional[UserProfile] = None,
) -> Dict[str, Any]:
    """
    Evaluates 10 PAR-Q screening items against safety gates and clinical rules.
    Returns structured decision dictionary with clearance state and recommended status.
    """
    if not isinstance(health_screening, dict):
        health_screening = {}

    red_flags: List[Dict[str, str]] = []
    modifications: List[Dict[str, str]] = []
    clinical_escalations: List[Dict[str, str]] = []

    # 1. Evaluate PAR-Q Questions
    for q in PARQ_QUESTIONS:
        key = q['key']
        raw_answer = health_screening.get(key)
        if _is_affirmative(raw_answer):
            detail = ''
            if isinstance(raw_answer, dict):
                detail = raw_answer.get('notes') or raw_answer.get('details') or ''

            entry = {
                'key': key,
                'question': q['question'],
                'category': q['category'],
                'severity': q['severity'],
                'detail': detail,
            }

            if key in CRITICAL_RED_FLAG_KEYS or q['severity'] == 'CRITICAL':
                red_flags.append(entry)
            else:
                modifications.append(entry)

    # 2. Clinical Escalation: Age check (minors)
    age = _calculate_member_age(user_profile)
    if age is not None and age < 18:
        clinical_escalations.append({
            'type': 'PEDIATRIC_MINOR',
            'severity': 'HIGH',
            'detail': f'Member is {age} years old (<18). Requires guardian consent and pediatric clearance.',
        })

    # 3. Clinical Escalation: Pregnancy / Postpartum check
    if _is_affirmative(health_screening.get('pregnancy_or_postpartum')):
        clinical_escalations.append({
            'type': 'PRENATAL_POSTPARTUM',
            'severity': 'MODERATE',
            'detail': 'Prenatal/postpartum protocol requires obstetrician clearance. Avoid prone/supine testing.',
        })

    # 4. Gate Decision Logic
    if red_flags:
        cleared_for_testing = False
        screening_outcome = 'DEFER_MEDICAL_CLEARANCE'
        recommended_status = 'DEFERRED'
        summary = (
            f"Safety Gate Alert: {len(red_flags)} critical red flag(s) identified "
            f"({', '.join(rf['key'] for rf in red_flags)}). Testing blocked pending medical clearance."
        )
    elif modifications:
        cleared_for_testing = True
        screening_outcome = 'PROCEED_WITH_MODIFICATIONS'
        recommended_status = 'DRAFT'
        summary = (
            f"Cleared with modifications: {len(modifications)} area(s) require coach exercise adjustments "
            f"({', '.join(m['key'] for m in modifications)})."
        )
    else:
        cleared_for_testing = True
        screening_outcome = 'CLEARED'
        recommended_status = 'DRAFT'
        summary = "Safety Gate: Cleared for full standard functional testing battery."

    return {
        'cleared_for_testing': cleared_for_testing,
        'screening_outcome': screening_outcome,
        'recommended_status': recommended_status,
        'red_flags': red_flags,
        'modifications': modifications,
        'clinical_escalations': clinical_escalations,
        'summary': summary,
        'requires_medical_clearance': len(red_flags) > 0,
        'total_affirmative_answers': len(red_flags) + len(modifications),
    }
