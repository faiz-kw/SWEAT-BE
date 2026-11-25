"""
apps/tenant_core/assessment_test_library.py — the studio's assessment test catalogue.

The module specification asks for "one common assessment library with adaptive protocols,
rather than a completely different program for every age". This is that library: every test
the battery can run, the fields it records, and the protocol version it was recorded under.

Why a catalogue rather than hardcoded form fields:

  - A 60-year-old and a trained 25-year-old need different tests for the same domain. The
    coach picks the appropriate protocol; the domain is the same either way.
  - Section 15 only permits comparing like with like. Every result therefore carries its
    `test_code` and `protocol_version`, so a reassessment can tell whether the same test was
    repeated or a different one was substituted.
  - The specification is explicit that inability to perform an advanced variation is not a
    deficiency. Tests carry a `difficulty` so a wall push-up and a floor push-up are read on
    the same scale instead of the easier one scoring as failure.

Field types are kept deliberately small — number, seconds, select, text, bool — because this
drives a form on a tablet, not a general query language.
"""

from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = '1.0'

# Shared option sets.
QUALITY_OPTIONS = ['Meets criteria', 'Needs cueing', 'Modified', 'Unable to assess']
STATUS_OPTIONS = ['NOT_ASSESSED', 'VALID', 'MODIFIED', 'INCOMPLETE']
SUPPORT_OPTIONS = ['None', 'Support available', 'Hands-on assistance']
SCREEN_OPTIONS = ['Comfortable', 'Modified', 'Restricted']


def _field(key: str, label: str, kind: str, **extra) -> Dict[str, Any]:
    field = {'key': key, 'label': label, 'type': kind}
    field.update(extra)
    return field


# Every test records quality, symptoms and a validity status; declared once.
COMMON_FIELDS = [
    _field('movement_quality', 'Movement quality', 'select', options=QUALITY_OPTIONS),
    _field('symptoms', 'Symptoms', 'text', placeholder='None'),
    _field('status', 'Result status', 'select', options=STATUS_OPTIONS, default='NOT_ASSESSED'),
]


TEST_LIBRARY: List[Dict[str, Any]] = [
    # ---------------------------------------------------------------- lower body
    {
        'code': 'SQUAT', 'domain': 'lower_body', 'name': 'Squat', 'section': '5A',
        'difficulty': 'scaled',
        'variations': ['Supported squat', 'Box squat', 'Bodyweight squat', 'Goblet squat'],
        'fields': [
            _field('box_height_cm', 'Box / target height (cm)', 'number'),
            _field('load_kg', 'External load (kg)', 'number'),
            _field('reps', 'Valid repetitions', 'number'),
        ],
    },
    {
        'code': 'CHAIR_STAND', 'domain': 'lower_body', 'name': '30-second chair stand', 'section': '5B',
        'difficulty': 'accessible',
        'variations': ['Arms crossed', 'Hands permitted'],
        'fields': [
            _field('chair_height_cm', 'Chair height (cm)', 'number'),
            _field('reps', 'Completed repetitions', 'number'),
            _field('used_hands', 'Used hands', 'bool'),
        ],
    },
    {
        'code': 'STEP_UP', 'domain': 'lower_body', 'name': 'Step-up', 'section': '5C',
        'difficulty': 'scaled',
        'variations': ['Rail support', 'Coach assistance', 'Unsupported'],
        'fields': [
            _field('step_height_cm', 'Step height (cm)', 'number'),
            _field('reps_left', 'Reps left', 'number'),
            _field('reps_right', 'Reps right', 'number'),
            _field('support_used', 'Support used', 'select', options=SUPPORT_OPTIONS),
        ],
    },
    {
        'code': 'SINGLE_LEG_SIT_STAND', 'domain': 'lower_body', 'name': 'Single-leg sit-to-stand', 'section': '5D',
        'difficulty': 'advanced',
        'variations': ['Single-leg sit-to-stand', 'Controlled split squat'],
        'fields': [
            _field('reps_left', 'Reps left', 'number'),
            _field('reps_right', 'Reps right', 'number'),
            _field('load_kg', 'Load (kg)', 'number'),
        ],
    },

    # ---------------------------------------------------------------- upper body
    {
        'code': 'PUSH_UP', 'domain': 'upper_body', 'name': 'Push-up', 'section': '6A',
        'difficulty': 'scaled',
        'variations': ['Wall push-up', 'High-incline push-up', 'Low-incline push-up',
                       'Knee push-up', 'Floor push-up', 'Advanced variation'],
        'fields': [
            _field('incline_height_cm', 'Surface / incline height (cm)', 'number'),
            _field('reps', 'Valid repetitions', 'number'),
            _field('range_of_motion', 'Range of motion', 'select',
                   options=['Full prescribed range', 'Modified']),
        ],
    },
    {
        'code': 'PULL_UP', 'domain': 'upper_body', 'name': 'Pull-up', 'section': '6B',
        'difficulty': 'advanced',
        'variations': ['Band-assisted', 'Machine-assisted', 'Unassisted', 'Not suitable'],
        'fields': [
            _field('assistance_kg', 'Assistance load (kg)', 'number'),
            _field('reps', 'Valid repetitions', 'number'),
            _field('grip', 'Grip', 'select', options=['Pronated', 'Supinated', 'Neutral']),
        ],
        # The specification is explicit: no pull-up is not a deficiency.
        'note': 'Record the variation and assistance level. Inability to perform is not scored negatively.',
    },
    {
        'code': 'ROW', 'domain': 'upper_body', 'name': 'Row', 'section': '6C',
        'difficulty': 'accessible',
        'variations': ['Seated cable row', 'Resistance-band row', 'Machine row'],
        'fields': [
            _field('load_kg', 'Resistance / load (kg)', 'number'),
            _field('reps', 'Repetitions', 'number'),
        ],
    },
    {
        'code': 'GRIP', 'domain': 'upper_body', 'name': 'Grip strength', 'section': '6D',
        'difficulty': 'accessible',
        'variations': ['Dynamometer'],
        'fields': [
            _field('grip_left_kg', 'Left (kg)', 'number'),
            _field('grip_right_kg', 'Right (kg)', 'number'),
            _field('trials', 'Trials per hand', 'number'),
        ],
    },

    # ---------------------------------------------------------------------- core
    {
        'code': 'PLANK', 'domain': 'core', 'name': 'Plank', 'section': '7A',
        'difficulty': 'scaled',
        'variations': ['Wall plank', 'Elevated plank', 'Knee plank', 'Forearm plank'],
        'fields': [
            _field('hold_seconds', 'Hold (seconds)', 'seconds'),
            _field('stopping_reason', 'Reason for stopping', 'select',
                   options=['Technique limit', 'Fatigue', 'Discomfort', 'Time limit']),
        ],
    },
    {
        'code': 'SIDE_PLANK', 'domain': 'core', 'name': 'Side plank', 'section': '7B',
        'difficulty': 'scaled',
        'variations': ['Modified', 'Standard'],
        'fields': [
            _field('hold_left_seconds', 'Left (seconds)', 'seconds'),
            _field('hold_right_seconds', 'Right (seconds)', 'seconds'),
        ],
    },
    {
        'code': 'FARMERS_CARRY', 'domain': 'core', 'name': "Farmer's carry", 'section': '7C',
        'difficulty': 'scaled',
        'variations': ['Suitcase carry', 'Farmer\'s carry'],
        'fields': [
            _field('load_per_hand_kg', 'Load per hand (kg)', 'number'),
            _field('distance_m', 'Distance (m)', 'number'),
            _field('time_seconds', 'Time (seconds)', 'seconds'),
        ],
    },

    # ------------------------------------------------------------------- balance
    {
        'code': 'SINGLE_LEG_BALANCE', 'domain': 'balance', 'name': 'Single-leg balance', 'section': '8A',
        'difficulty': 'scaled',
        'variations': ['Eyes open', 'Other protocol'],
        'fields': [
            _field('single_leg_left_sec', 'Left (seconds)', 'seconds'),
            _field('single_leg_right_sec', 'Right (seconds)', 'seconds'),
            _field('max_duration_sec', 'Maximum test duration (seconds)', 'seconds'),
            _field('support_used', 'Support used', 'select', options=SUPPORT_OPTIONS),
        ],
    },
    {
        'code': 'TANDEM_STANCE', 'domain': 'balance', 'name': 'Tandem stance', 'section': '8B',
        'difficulty': 'accessible',
        'variations': ['Heel-to-toe', 'Modified'],
        'fields': [
            _field('hold_seconds', 'Duration (seconds)', 'seconds'),
            _field('support_used', 'Support used', 'select', options=SUPPORT_OPTIONS),
            _field('balance_loss', 'Balance loss', 'bool'),
        ],
    },
    {
        'code': 'TUG', 'domain': 'balance', 'name': 'Timed up and go', 'section': '8C',
        'difficulty': 'accessible',
        'variations': ['Standard TUG'],
        'fields': [
            _field('chair_height_cm', 'Chair height (cm)', 'number'),
            _field('walk_distance_m', 'Walking distance (m)', 'number'),
            _field('time_seconds', 'Completion time (seconds)', 'seconds'),
            _field('walking_aid', 'Walking aid', 'text'),
        ],
        'note': 'Use a consistent protocol and appropriate reference values. Not required for every member.',
    },
    {
        'code': 'FLOOR_TO_STAND', 'domain': 'balance', 'name': 'Floor-to-stand transfer', 'section': '8D',
        'difficulty': 'accessible',
        'variations': ['Independent', 'With support'],
        'fields': [
            _field('completed_independently', 'Completed independently', 'bool'),
            _field('time_seconds', 'Time (seconds)', 'seconds'),
            _field('support_used', 'Support used', 'select', options=SUPPORT_OPTIONS),
        ],
    },

    # ------------------------------------------------------------------ mobility
    {
        'code': 'ANKLE_KNEE_TO_WALL', 'domain': 'mobility', 'name': 'Ankle knee-to-wall', 'section': '9A',
        'difficulty': 'accessible',
        'variations': ['Knee-to-wall'],
        'fields': [
            _field('ankle_left_cm', 'Left distance (cm)', 'number'),
            _field('ankle_right_cm', 'Right distance (cm)', 'number'),
            _field('heel_maintained', 'Heel stayed down', 'bool'),
        ],
    },
    {
        'code': 'SHOULDER_MOBILITY', 'domain': 'mobility', 'name': 'Shoulder mobility', 'section': '9B',
        'difficulty': 'accessible',
        'variations': ['Reach test', 'Other'],
        'fields': [
            _field('shoulder_mobility', 'Result', 'select', options=SCREEN_OPTIONS),
            _field('left_result', 'Left', 'text'),
            _field('right_result', 'Right', 'text'),
        ],
    },
    {
        'code': 'HAMSTRING_REACH', 'domain': 'mobility', 'name': 'Hamstring / posterior chain', 'section': '9C',
        'difficulty': 'accessible',
        'variations': ['Sit-and-reach', 'Alternative test'],
        'fields': [
            _field('hamstring_reach', 'Result', 'select', options=SCREEN_OPTIONS),
            _field('sit_and_reach_cm', 'Distance (cm)', 'number'),
        ],
    },
    {
        'code': 'OVERHEAD_SQUAT_SCREEN', 'domain': 'mobility', 'name': 'Overhead reach & squat screen', 'section': '9D',
        'difficulty': 'accessible',
        'variations': ['Standard screen'],
        'fields': [
            _field('overhead_reach', 'Overhead reach', 'select', options=SCREEN_OPTIONS),
            _field('squat_pattern', 'Squat pattern', 'select', options=SCREEN_OPTIONS),
            _field('limiting_factor', 'Limiting factor', 'select',
                   options=['Mobility', 'Balance', 'Strength', 'Coordination', 'Discomfort', 'Unclear']),
        ],
    },

    # ------------------------------------------------------------------- cardio
    {
        'code': 'TREADMILL', 'domain': 'cardiovascular', 'name': 'Treadmill walk', 'section': '10A',
        'difficulty': 'scaled',
        'variations': ['Comfortable walk', 'Standardised submaximal walk', 'Walk with incline', 'Jog'],
        'fields': [
            _field('duration_minutes', 'Duration (min)', 'number'),
            _field('speed_kmh', 'Speed (km/h)', 'number'),
            _field('incline_pct', 'Incline (%)', 'number'),
            _field('hr_before_bpm', 'HR before (bpm)', 'number'),
            _field('hr_after_bpm', 'HR after (bpm)', 'number'),
            _field('recovery_hr_bpm', 'Recovery HR (bpm)', 'number'),
            _field('perceived_exertion', 'Perceived exertion (0-10)', 'number'),
            _field('talk_test', 'Talk test', 'select',
                   options=['Comfortable', 'Somewhat difficult', 'Difficult']),
        ],
    },
    {
        'code': 'STEP_TEST_2MIN', 'domain': 'cardiovascular', 'name': '2-minute step test', 'section': '10B',
        'difficulty': 'accessible',
        'variations': ['Standard step height rule'],
        'fields': [
            _field('repetitions', 'Valid repetitions', 'number'),
            _field('support_used', 'Support used', 'select', options=SUPPORT_OPTIONS),
            _field('perceived_exertion', 'Perceived exertion (0-10)', 'number'),
        ],
    },
    {
        'code': 'WALK_6MIN', 'domain': 'cardiovascular', 'name': '6-minute walk test', 'section': '10C',
        'difficulty': 'accessible',
        'variations': ['Standard course'],
        'fields': [
            _field('distance_m', 'Distance (m)', 'number'),
            _field('rest_breaks', 'Rest breaks', 'number'),
            _field('walking_aid', 'Walking aid', 'text'),
            _field('perceived_exertion', 'Perceived exertion (0-10)', 'number'),
        ],
    },
    {
        'code': 'CYCLE', 'domain': 'cardiovascular', 'name': 'Stationary cycle', 'section': '10D',
        'difficulty': 'accessible',
        'variations': ['Submaximal cycle'],
        'fields': [
            _field('duration_minutes', 'Duration (min)', 'number'),
            _field('workload', 'Resistance / workload', 'text'),
            _field('cadence', 'Cadence', 'number'),
            _field('hr_after_bpm', 'HR after (bpm)', 'number'),
            _field('perceived_exertion', 'Perceived exertion (0-10)', 'number'),
        ],
    },

    # ------------------------------------------- section 11: optional performance
    {
        'code': 'LOADED_CARRY', 'domain': 'performance', 'name': 'Loaded carry', 'section': '11',
        'difficulty': 'advanced',
        'variations': ['Suitcase', 'Front rack', 'Overhead'],
        'fields': [
            _field('load_kg', 'Load (kg)', 'number'),
            _field('distance_m', 'Distance (m)', 'number'),
            _field('time_seconds', 'Time (seconds)', 'seconds'),
        ],
    },
    {
        'code': 'MED_BALL_THROW', 'domain': 'performance', 'name': 'Medicine-ball throw', 'section': '11',
        'difficulty': 'advanced',
        'variations': ['Chest pass', 'Rotational', 'Overhead'],
        'fields': [
            _field('ball_kg', 'Ball weight (kg)', 'number'),
            _field('distance_m', 'Distance (m)', 'number'),
            _field('attempts', 'Attempts', 'number'),
        ],
    },
    {
        'code': 'JUMP', 'domain': 'performance', 'name': 'Jump test', 'section': '11',
        'difficulty': 'advanced',
        'variations': ['Countermovement jump', 'Broad jump'],
        'fields': [
            _field('height_cm', 'Height (cm)', 'number'),
            _field('distance_cm', 'Distance (cm)', 'number'),
            _field('attempts', 'Attempts', 'number'),
        ],
        'note': 'Never a universal requirement. Only when it suits the member and their goal.',
    },
    {
        'code': 'SPORT_SPECIFIC', 'domain': 'performance', 'name': 'Sport-specific movement', 'section': '11',
        'difficulty': 'advanced',
        'variations': ['Coach-defined'],
        'fields': [
            _field('protocol', 'Protocol used', 'text'),
            _field('equipment', 'Equipment', 'text'),
            _field('result', 'Result', 'text'),
        ],
    },
]


DOMAIN_LABELS = {
    'warm_up_observation': 'Warm-up',
    'lower_body': 'Lower body',
    'upper_body': 'Upper body',
    'core': 'Core',
    'balance': 'Balance',
    'mobility': 'Mobility',
    'cardiovascular': 'Cardio',
    'performance': 'Performance (optional)',
}


def tests_for_domain(domain: str) -> List[Dict[str, Any]]:
    return [t for t in TEST_LIBRARY if t['domain'] == domain]


def get_test(code: str) -> Optional[Dict[str, Any]]:
    for test in TEST_LIBRARY:
        if test['code'] == code:
            return test
    return None


def library_payload() -> Dict[str, Any]:
    """
    The catalogue as the tablet consumes it: domains in running order, each with its tests.
    Served rather than duplicated in the client so both sides agree on protocol versions.
    """
    ordered = ['warm_up_observation', 'lower_body', 'upper_body', 'core',
               'balance', 'mobility', 'cardiovascular', 'performance']
    return {
        'protocol_version': PROTOCOL_VERSION,
        'status_options': STATUS_OPTIONS,
        'domains': [
            {
                'key': domain,
                'label': DOMAIN_LABELS[domain],
                'optional': domain == 'performance',
                'tests': tests_for_domain(domain),
            }
            for domain in ordered
        ],
    }
