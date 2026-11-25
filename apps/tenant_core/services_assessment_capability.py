"""
apps/tenant_core/services_assessment_capability.py — Section 13 capability matrix.

Turns the logged movement battery into one capability label per domain.

Two rules govern everything here:

1.  A domain with no usable data reports NOT_ASSESSED. It never falls back to a flattering
    default. Before this module existed, mobility, cardiovascular and functional_movement were
    hardcoded to "Within Protocol", "Establishing Baseline" and "Independent" — three of seven
    domains on the coach's screen and the studio TV were fabricated, because no input for them
    was ever collected.

2.  Each domain uses the vocabulary the specification defines for it. Strength domains run
    Beginner / Developing / Established; balance runs Needs support / Independent under test
    conditions; mobility runs Within protocol / Modified / Further assessment needed; cardio
    describes the protocol actually completed. They are deliberately not forced onto one
    scale, because they do not measure comparable things.

A test marked incomplete or not assessed contributes nothing, and reported pain downgrades a
domain to "Further assessment needed" rather than being averaged away.
"""

from typing import Any, Dict, List, Optional

NOT_ASSESSED = 'Not assessed'

# Strength and endurance domains (specification section 13).
BEGINNER = 'Beginner'
DEVELOPING = 'Developing'
ESTABLISHED = 'Established'

# Balance.
NEEDS_SUPPORT = 'Needs support'
INDEPENDENT_UNDER_TEST = 'Independent under test conditions'

# Mobility.
WITHIN_PROTOCOL = 'Within protocol'
MODIFIED = 'Modified'
FURTHER_ASSESSMENT = 'Further assessment needed'

# Functional movement.
INDEPENDENT = 'Independent'
ASSISTANCE_NEEDED = 'Assistance needed'

DOMAINS = (
    'lower_body', 'upper_body', 'core', 'balance',
    'mobility', 'cardiovascular', 'functional_movement',
)

# A result marked any of these carries no capability signal.
UNUSABLE_STATUSES = {'NOT_ASSESSED', 'NOT ASSESSED', 'INCOMPLETE', ''}


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    v = _as_float(value)
    return int(v) if v is not None else None


def _is_usable(entry: Dict[str, Any]) -> bool:
    """A test counts only when it was actually completed under a known protocol."""
    status = str(
        entry.get('result_status') or entry.get('validity') or entry.get('status') or ''
    ).strip().upper().replace('-', '_')
    if status in UNUSABLE_STATUSES and status != '':
        return False
    return True


def _has_pain(entry: Dict[str, Any]) -> bool:
    if entry.get('pain_reported') is True:
        return True
    symptoms = entry.get('symptoms') or entry.get('reported_symptoms') or ''
    if isinstance(symptoms, dict):
        return bool(symptoms)
    text = str(symptoms).strip().lower()
    return bool(text) and text not in ('none', 'no', 'nil', 'n/a', '-')


def _tests_in(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """
    Normalises the three shapes the battery arrives in: a list under `assessed_tests`, a bare
    list, or a single dict for the one-test-per-domain case.
    """
    section = payload.get(key)
    if isinstance(section, list):
        candidates = section
    elif isinstance(section, dict):
        if isinstance(section.get('assessed_tests'), list):
            candidates = section['assessed_tests']
        elif section:
            candidates = [section]
        else:
            candidates = []
    else:
        candidates = []

    out = []
    for item in candidates:
        if not isinstance(item, dict) or not item:
            continue
        if item.get('active') is False:
            continue
        if not _is_usable(item):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Domain evaluators
# ---------------------------------------------------------------------------

def _code(entry: Dict[str, Any]) -> str:
    return str(entry.get('test_code') or entry.get('code') or '').strip().upper()


def evaluate_lower_body(payload: Dict[str, Any]) -> str:
    """
    Squat, chair stand, step-up or single-leg sit-to-stand — whichever the coach selected.

    Each test has its own thresholds; a chair-stand count and a squat rep count are not the
    same measurement. The strongest usable finding wins, because a member who completes an
    advanced test has demonstrated the capability regardless of how a scaled one read.
    """
    tests = _tests_in(payload, 'lower_body')
    if not tests:
        return NOT_ASSESSED
    if any(_has_pain(t) for t in tests):
        return FURTHER_ASSESSMENT

    levels = []
    for t in tests:
        code = _code(t)
        name = str(t.get('name') or t.get('variation') or t.get('test_name') or '').lower()
        reps = _as_int(t.get('reps') or t.get('repetitions')) or 0
        quality_ok = str(t.get('movement_quality') or '').strip().lower() in (
            'meets criteria', 'good control', 'controlled')

        if code == 'CHAIR_STAND':
            # Repetitions in 30 seconds. Using hands is itself a support signal.
            if t.get('used_hands') is True:
                levels.append(BEGINNER)
            elif reps >= 15:
                levels.append(ESTABLISHED)
            elif reps >= 9:
                levels.append(DEVELOPING)
            else:
                levels.append(BEGINNER)
            continue

        if code == 'STEP_UP':
            per_side = min(_as_int(t.get('reps_left')) or 0, _as_int(t.get('reps_right')) or 0)
            support = str(t.get('support_used') or '').strip().lower()
            if support in ('hands-on assistance', 'coach assistance'):
                levels.append(BEGINNER)
            elif per_side >= 12:
                levels.append(ESTABLISHED)
            elif per_side >= 6:
                levels.append(DEVELOPING)
            else:
                levels.append(BEGINNER)
            continue

        if code == 'SINGLE_LEG_SIT_STAND':
            # An advanced variation performed at all demonstrates established capability.
            per_side = min(_as_int(t.get('reps_left')) or 0, _as_int(t.get('reps_right')) or 0)
            levels.append(ESTABLISHED if per_side >= 3 else DEVELOPING)
            continue

        # Squat (default)
        if (reps >= 15 or 'goblet' in name) and quality_ok:
            levels.append(ESTABLISHED)
        elif 'box' in name or 'supported' in name or reps < 8:
            levels.append(BEGINNER)
        else:
            levels.append(DEVELOPING)

    return _best_of(levels)


def _best_of(levels: List[str]) -> str:
    """Highest demonstrated level across the tests run for a domain."""
    order = [NOT_ASSESSED, BEGINNER, DEVELOPING, ESTABLISHED]
    best = NOT_ASSESSED
    for level in levels:
        if level in order and order.index(level) > order.index(best):
            best = level
    return best or NOT_ASSESSED


def evaluate_upper_body(payload: Dict[str, Any]) -> str:
    """
    Push-up, pull-up, row or grip strength.

    The specification is explicit that a member who cannot perform a pull-up is not thereby
    unfit, so an attempted pull-up never lowers the domain — it can only raise it.
    """
    tests = _tests_in(payload, 'upper_body')
    if not tests:
        return NOT_ASSESSED
    if any(_has_pain(t) for t in tests):
        return FURTHER_ASSESSMENT

    levels = []
    for t in tests:
        code = _code(t)
        name = str(t.get('name') or t.get('pushup_variation') or t.get('variation') or '').lower()
        reps = _as_int(t.get('pushup_reps') or t.get('reps')) or 0

        if code == 'PULL_UP':
            assistance = _as_float(t.get('assistance_kg')) or 0.0
            if reps >= 1 and assistance == 0 and 'unassisted' in name:
                levels.append(ESTABLISHED)
            elif reps >= 5:
                levels.append(DEVELOPING)
            # Otherwise contributes nothing: an advanced test not yet achievable is not a
            # deficiency, it just does not raise the level.
            continue

        if code == 'ROW':
            if reps >= 12:
                levels.append(DEVELOPING)
            elif reps > 0:
                levels.append(BEGINNER)
            continue

        if code == 'GRIP':
            # Grip is recorded for trend, not graded: published norms vary by age and sex and
            # the studio has not adopted a reference set.
            continue

        # Push-up (default)
        if 'floor' in name and reps >= 12:
            levels.append(ESTABLISHED)
        elif 'wall' in name or reps < 6:
            levels.append(BEGINNER)
        else:
            levels.append(DEVELOPING)

    return _best_of(levels)


def evaluate_core(payload: Dict[str, Any]) -> str:
    section = _as_dict(payload.get('core'))
    if not section or not _is_usable(section):
        return NOT_ASSESSED
    hold = _as_int(section.get('hold_seconds'))
    if not hold:
        return NOT_ASSESSED
    if _has_pain(section):
        return FURTHER_ASSESSMENT
    if hold < 25:
        return BEGINNER
    if hold >= 60:
        return ESTABLISHED
    return DEVELOPING


def evaluate_core_battery(payload: Dict[str, Any]) -> str:
    """Core across plank, side plank and loaded carry, taking the strongest finding."""
    tests = _tests_in(payload, 'core')
    if not tests:
        return evaluate_core(payload)
    if any(_has_pain(t) for t in tests):
        return FURTHER_ASSESSMENT

    levels = []
    for t in tests:
        code = _code(t)
        if code == 'SIDE_PLANK':
            worst = min(_as_float(t.get('hold_left_seconds')) or 0.0,
                        _as_float(t.get('hold_right_seconds')) or 0.0)
            if worst >= 45:
                levels.append(ESTABLISHED)
            elif worst >= 20:
                levels.append(DEVELOPING)
            elif worst > 0:
                levels.append(BEGINNER)
            continue
        if code == 'FARMERS_CARRY':
            distance = _as_float(t.get('distance_m')) or 0.0
            if distance >= 40:
                levels.append(ESTABLISHED)
            elif distance >= 15:
                levels.append(DEVELOPING)
            elif distance > 0:
                levels.append(BEGINNER)
            continue
        hold = _as_int(t.get('hold_seconds'))
        if hold:
            levels.append(BEGINNER if hold < 25 else (ESTABLISHED if hold >= 60 else DEVELOPING))

    return _best_of(levels) if levels else NOT_ASSESSED


def evaluate_balance(payload: Dict[str, Any]) -> str:
    """
    Single-leg balance, tandem stance, timed-up-and-go or floor-to-stand.

    Any test requiring hands-on assistance sets the domain to "Needs support" regardless of
    what another test showed: support is the safety-relevant finding.
    """
    tests = _tests_in(payload, 'balance')
    if not tests:
        return NOT_ASSESSED
    if any(_has_pain(t) for t in tests):
        return FURTHER_ASSESSMENT

    verdicts = []
    for t in tests:
        code = _code(t)
        support = str(t.get('support_used') or '').strip().lower()
        if support in ('hands-on assistance', 'hands on assistance', 'coach assistance'):
            return NEEDS_SUPPORT

        if code == 'TUG':
            seconds = _as_float(t.get('time_seconds'))
            if seconds is None:
                continue
            # A widely used screening cut-off; recorded as a studio threshold, not a
            # clinical fall-risk diagnosis.
            verdicts.append(NEEDS_SUPPORT if seconds > 12.0 else INDEPENDENT_UNDER_TEST)
            continue

        if code == 'FLOOR_TO_STAND':
            if t.get('completed_independently') is False:
                return NEEDS_SUPPORT
            if t.get('completed_independently') is True:
                verdicts.append(INDEPENDENT_UNDER_TEST)
            continue

        if code == 'TANDEM_STANCE':
            hold = _as_float(t.get('hold_seconds'))
            if hold is None:
                continue
            if t.get('balance_loss') is True:
                verdicts.append(NEEDS_SUPPORT)
            else:
                verdicts.append(NEEDS_SUPPORT if hold < 10 else INDEPENDENT_UNDER_TEST)
            continue

        left = _as_float(t.get('single_leg_left_sec'))
        right = _as_float(t.get('single_leg_right_sec'))
        if left is None and right is None:
            continue
        worst = min(v for v in (left, right) if v is not None)
        # 15 seconds unsupported is the studio's threshold for independent standing balance
        # under test conditions. It is not a clinical fall-risk judgement.
        verdicts.append(NEEDS_SUPPORT if worst < 15 else INDEPENDENT_UNDER_TEST)

    if not verdicts:
        return NOT_ASSESSED
    # The cautious reading wins when tests disagree.
    return NEEDS_SUPPORT if NEEDS_SUPPORT in verdicts else INDEPENDENT_UNDER_TEST


def evaluate_mobility(payload: Dict[str, Any]) -> str:
    """
    Section 9. Screens are pass/modify/restrict judgements plus optional measurements; the
    specification is explicit that a mobility screen is not a diagnosis, so the worst
    observation sets the level rather than an average.
    """
    # Accepts either a single mobility block or the per-test list the catalogue produces;
    # merging them lets one evaluator read both shapes.
    tests = _tests_in(payload, 'mobility')
    if not tests:
        return NOT_ASSESSED
    section: Dict[str, Any] = {}
    for t in tests:
        section.update({k: v for k, v in t.items() if v not in (None, '')})

    screens = [
        section.get('shoulder_mobility'),
        section.get('ankle_knee_to_wall'),
        section.get('hamstring_reach'),
        section.get('overhead_reach'),
        section.get('squat_pattern'),
    ]
    verdicts = [str(s).strip().lower() for s in screens if s not in (None, '')]

    measurements = [
        _as_float(section.get('ankle_left_cm')), _as_float(section.get('ankle_right_cm')),
        _as_float(section.get('sit_and_reach_cm')),
    ]
    has_measurement = any(m is not None for m in measurements)

    if not verdicts and not has_measurement:
        return NOT_ASSESSED

    if _has_pain(section) or any(v in ('restricted', 'painful', 'pain reported') for v in verdicts):
        return FURTHER_ASSESSMENT
    if any(v in ('modified', 'limited') for v in verdicts):
        return MODIFIED

    # A marked left/right difference is worth a coach's attention even when each side passes.
    left, right = _as_float(section.get('ankle_left_cm')), _as_float(section.get('ankle_right_cm'))
    if left is not None and right is not None and abs(left - right) >= 4.0:
        return MODIFIED

    return WITHIN_PROTOCOL


def evaluate_cardiovascular(payload: Dict[str, Any]) -> str:
    """
    Section 10. The specification says a cardio result is only meaningful next to the protocol
    that produced it, so the label names the protocol rather than grading the member.
    """
    tests = _tests_in(payload, 'cardiovascular') or _tests_in(payload, 'cardio')
    if not tests:
        return NOT_ASSESSED
    # The most recently recorded cardio test describes the baseline.
    section = tests[-1]

    protocol = (
        str(section.get('protocol') or section.get('name')
            or section.get('test_name') or '').strip()
    )
    has_result = any(_as_float(section.get(k)) is not None for k in (
        'duration_minutes', 'distance_km', 'distance_m', 'steps', 'repetitions',
        'recovery_hr_bpm', 'perceived_exertion',
    ))
    if not protocol and not has_result:
        return NOT_ASSESSED

    if _has_pain(section):
        return FURTHER_ASSESSMENT

    status = str(section.get('result_status') or '').strip().upper()
    if status == 'MODIFIED':
        return f'Modified protocol — {protocol}' if protocol else MODIFIED

    return f'Baseline recorded — {protocol}' if protocol else 'Baseline recorded'


def evaluate_functional_movement(payload: Dict[str, Any]) -> str:
    """
    Section 4 warm-up observation: can the member perform everyday patterns unaided.
    """
    section = _as_dict(payload.get('warm_up_observation')) or _as_dict(payload.get('functional_movement'))
    if not section or not _is_usable(section):
        return NOT_ASSESSED

    observations = [
        section.get('walking'), section.get('sit_to_stand'), section.get('squat_pattern'),
        section.get('overhead_reach'), section.get('step_up'),
    ]
    verdicts = [str(o).strip().lower() for o in observations if o not in (None, '')]
    if not verdicts:
        return NOT_ASSESSED

    if _has_pain(section) or any('pain' in v for v in verdicts):
        return FURTHER_ASSESSMENT
    if any(v in ('unable today', 'unable', 'uses support', 'assistance', 'restricted') for v in verdicts):
        return ASSISTANCE_NEEDED
    if any(v in ('modified', 'needs cueing') for v in verdicts):
        return MODIFIED
    return INDEPENDENT


EVALUATORS = {
    'lower_body': evaluate_lower_body,
    'upper_body': evaluate_upper_body,
    'core': evaluate_core_battery,
    'balance': evaluate_balance,
    'mobility': evaluate_mobility,
    'cardiovascular': evaluate_cardiovascular,
    'functional_movement': evaluate_functional_movement,
}


def evaluate_capability_matrix(tests_payload: Any) -> Dict[str, str]:
    """
    Returns one label per domain. Domains without usable data report NOT_ASSESSED, which is a
    different statement from a low score and must stay distinguishable downstream.
    """
    payload = _as_dict(tests_payload)
    matrix: Dict[str, str] = {}
    for domain in DOMAINS:
        try:
            matrix[domain] = EVALUATORS[domain](payload)
        except Exception:
            # A malformed entry for one domain must not take the whole matrix down.
            matrix[domain] = NOT_ASSESSED
    return matrix


def assessed_domain_count(matrix: Dict[str, str]) -> int:
    """How many domains carry a real finding — used to show coverage in the UI."""
    return sum(1 for d in DOMAINS if (matrix or {}).get(d, NOT_ASSESSED) != NOT_ASSESSED)
