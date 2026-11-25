"""
apps/tenant_core/services_assessment_timeline.py — the transformation timeline, as a range.

The module specification is explicit on two points:

    "The system should then produce an estimated range, with the assumptions visible to the
     coach and member. Recalculate it at reassessment using the actual trend in weight,
     circumference, performance, training attendance and nutrition adherence."

    "Don't promise a specific appearance by a fixed date."

So this never returns a single date. It returns a low and high week count, the assumptions
behind them, and — at reassessment — a revision based on what actually happened rather than
what was planned.

The arithmetic is deliberately simple and stated in full. A member is entitled to know that
the estimate came from a rate assumption and their own attendance, not from a model nobody
can inspect.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional

# Conservative and optimistic rates of change, per week, for a member training consistently.
# Sources are the studio's own planning assumptions, not clinical guidance, and they are
# surfaced with every estimate so a coach can disagree with them.
RATE_ASSUMPTIONS = {
    'FAT_LOSS': {
        'metric': 'body weight',
        'slow_pct_per_week': 0.25,   # % of body weight
        'fast_pct_per_week': 0.75,
        'note': 'Assumes a moderate energy deficit and consistent attendance.',
    },
    'WAIST': {
        'metric': 'waist circumference',
        'slow_cm_per_week': 0.15,
        'fast_cm_per_week': 0.40,
        'note': 'Waist change lags weight change and is the steadier signal.',
    },
    'STRENGTH': {
        'metric': 'strength and conditioning',
        'slow_weeks_per_level': 10,
        'fast_weeks_per_level': 6,
        'note': 'Time to move one capability level with progressive overload.',
    },
}

# Below this many sessions a week, progress assumptions do not hold.
MIN_VIABLE_FREQUENCY = 2


def _weeks_for_weight_goal(
    current_kg: float, target_kg: float, frequency_days: int,
) -> Optional[Dict[str, Any]]:
    delta = current_kg - target_kg
    if delta <= 0:
        return None

    rates = RATE_ASSUMPTIONS['FAT_LOSS']
    slow_kg = current_kg * (rates['slow_pct_per_week'] / 100.0)
    fast_kg = current_kg * (rates['fast_pct_per_week'] / 100.0)

    # Attendance scales the rate: the plan assumes three sessions a week.
    attendance_factor = min(1.0, max(0.4, (frequency_days or 3) / 3.0))
    slow_kg *= attendance_factor
    fast_kg *= attendance_factor

    if slow_kg <= 0 or fast_kg <= 0:
        return None

    return {
        'metric': rates['metric'],
        'change_required': round(delta, 1),
        'unit': 'kg',
        'weeks_low': int(round(delta / fast_kg)),
        'weeks_high': int(round(delta / slow_kg)),
        'assumptions': [
            f"{rates['slow_pct_per_week']}-{rates['fast_pct_per_week']}% of body weight per week.",
            f"Scaled for {frequency_days or 3} session(s) a week against a 3-a-week plan.",
            rates['note'],
        ],
    }


def estimate_timeline(
    *,
    goal: str,
    current_weight_kg: Optional[float] = None,
    target_weight_kg: Optional[float] = None,
    weekly_frequency_days: int = 3,
    committed_weeks: Optional[int] = None,
    observed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Returns a week range rather than a date, with every assumption attached.

    `observed` carries what actually happened since the last assessment — weight change,
    weeks elapsed, sessions attended — and when present it overrides the rate assumption with
    the member's own trend. That is the recalculation the specification asks for.
    """
    result: Dict[str, Any] = {
        'is_estimate': True,
        'basis': 'planned',
        'goal': goal,
        'weeks_low': None,
        'weeks_high': None,
        'committed_weeks': committed_weeks,
        'assumptions': [],
        'warnings': [],
        'disclosure': (
            'An estimated range, not a promise of a result by a date. It is recalculated at '
            'each reassessment from what actually changed.'
        ),
    }

    if (weekly_frequency_days or 0) < MIN_VIABLE_FREQUENCY:
        result['warnings'].append(
            f'At {weekly_frequency_days} session(s) a week the rate assumptions do not hold; '
            'treat any range as indicative only.'
        )

    # --- recalculation from observed trend ------------------------------------------------
    if observed:
        weeks_elapsed = float(observed.get('weeks_elapsed') or 0)
        weight_change = observed.get('weight_change_kg')
        remaining = observed.get('remaining_kg')
        if weeks_elapsed >= 2 and weight_change and remaining and float(remaining) > 0:
            actual_rate = abs(float(weight_change)) / weeks_elapsed
            if actual_rate > 0:
                projected = float(remaining) / actual_rate
                # A band around the observed rate, because a few weeks is a short sample.
                result.update({
                    'basis': 'observed',
                    'weeks_low': int(round(projected * 0.8)),
                    'weeks_high': int(round(projected * 1.3)),
                    'observed_rate_per_week': round(actual_rate, 3),
                    'assumptions': [
                        f'Based on {abs(float(weight_change)):.1f}kg over {weeks_elapsed:.0f} weeks '
                        f'({actual_rate:.2f}kg/week), not on a generic rate.',
                        'A band of -20% to +30% around that rate reflects the short sample.',
                    ],
                })
                attended = observed.get('sessions_attended')
                planned = observed.get('sessions_planned')
                if attended is not None and planned:
                    adherence = round(100.0 * float(attended) / float(planned))
                    result['attendance_pct'] = adherence
                    if adherence < 70:
                        result['warnings'].append(
                            f'Attendance is {adherence}% of plan. The range assumes it returns to plan.'
                        )
                return result

        result['warnings'].append(
            'Not enough observed change yet to recalculate; showing the planned estimate.'
        )

    # --- planned estimate -----------------------------------------------------------------
    if current_weight_kg and target_weight_kg:
        weight_band = _weeks_for_weight_goal(
            float(current_weight_kg), float(target_weight_kg), weekly_frequency_days,
        )
        if weight_band:
            result.update({
                'weeks_low': weight_band['weeks_low'],
                'weeks_high': weight_band['weeks_high'],
                'driver': weight_band['metric'],
                'change_required': weight_band['change_required'],
                'unit': weight_band['unit'],
                'assumptions': weight_band['assumptions'],
            })
            return result

    # No measurable target: fall back to the capability-progression assumption.
    rates = RATE_ASSUMPTIONS['STRENGTH']
    factor = min(1.0, max(0.5, (weekly_frequency_days or 3) / 3.0))
    result.update({
        'weeks_low': int(round(rates['fast_weeks_per_level'] / factor)),
        'weeks_high': int(round(rates['slow_weeks_per_level'] / factor)),
        'driver': rates['metric'],
        'assumptions': [
            f"{rates['fast_weeks_per_level']}-{rates['slow_weeks_per_level']} weeks to move one "
            'capability level.',
            f'Scaled for {weekly_frequency_days or 3} session(s) a week.',
            rates['note'],
        ],
    })
    return result


def timeline_for_assessment(assessment, previous=None) -> Dict[str, Any]:
    """Builds the range for one assessment, recalculating when a previous one is linked."""
    observed: Optional[Dict[str, Any]] = None

    if previous is not None and previous.measured_weight_kg and assessment.measured_weight_kg:
        created_now = getattr(assessment, 'created_at', None)
        created_then = getattr(previous, 'created_at', None)
        if created_now and created_then:
            weeks = max(0.0, (created_now - created_then).days / 7.0)
            change = float(previous.measured_weight_kg) - float(assessment.measured_weight_kg)
            target = getattr(assessment, 'target_weight_kg', None)
            remaining = (
                float(assessment.measured_weight_kg) - float(target)
                if target else None
            )
            observed = {
                'weeks_elapsed': weeks,
                'weight_change_kg': change,
                'remaining_kg': remaining,
            }

    return estimate_timeline(
        goal=assessment.target_goal,
        current_weight_kg=float(assessment.measured_weight_kg) if assessment.measured_weight_kg else None,
        target_weight_kg=float(getattr(assessment, 'target_weight_kg', 0) or 0) or None,
        weekly_frequency_days=assessment.weekly_frequency_days or 3,
        committed_weeks=assessment.target_timeline_weeks,
        observed=observed,
    )
