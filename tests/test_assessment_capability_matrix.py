"""
Tests for the Section 13 capability matrix.

The central guarantee is negative: a domain with no usable data must report "Not assessed".
Three domains (mobility, cardiovascular, functional movement) previously reported confident
labels on every assessment because nothing collected them, which is what these tests exist to
prevent coming back.
"""

import unittest

from apps.tenant_core.services_assessment_capability import (
    ASSISTANCE_NEEDED,
    BEGINNER,
    DEVELOPING,
    DOMAINS,
    ESTABLISHED,
    FURTHER_ASSESSMENT,
    INDEPENDENT,
    INDEPENDENT_UNDER_TEST,
    MODIFIED,
    NEEDS_SUPPORT,
    NOT_ASSESSED,
    WITHIN_PROTOCOL,
    assessed_domain_count,
    evaluate_capability_matrix,
)


class EmptyPayloadTests(unittest.TestCase):
    """Nothing logged means nothing claimed."""

    def test_empty_payload_reports_every_domain_unassessed(self):
        matrix = evaluate_capability_matrix({})
        for domain in DOMAINS:
            self.assertEqual(matrix[domain], NOT_ASSESSED, domain)

    def test_all_seven_domains_are_present(self):
        self.assertEqual(set(evaluate_capability_matrix({}).keys()), set(DOMAINS))

    def test_none_payload_is_safe(self):
        self.assertEqual(evaluate_capability_matrix(None)['mobility'], NOT_ASSESSED)

    def test_garbage_payload_is_safe(self):
        self.assertEqual(evaluate_capability_matrix('nonsense')['core'], NOT_ASSESSED)

    def test_the_three_formerly_hardcoded_domains_are_not_assessed(self):
        """Regression: these used to read Within Protocol / Establishing Baseline / Independent."""
        matrix = evaluate_capability_matrix({'lower_body': {'reps': 20}})
        self.assertEqual(matrix['mobility'], NOT_ASSESSED)
        self.assertEqual(matrix['cardiovascular'], NOT_ASSESSED)
        self.assertEqual(matrix['functional_movement'], NOT_ASSESSED)

    def test_assessed_count_reflects_real_coverage(self):
        self.assertEqual(assessed_domain_count(evaluate_capability_matrix({})), 0)
        matrix = evaluate_capability_matrix({'core': {'hold_seconds': 45}})
        self.assertEqual(assessed_domain_count(matrix), 1)


class StrengthDomainTests(unittest.TestCase):

    def test_strong_goblet_squat_is_established(self):
        matrix = evaluate_capability_matrix({
            'lower_body': {'variation': 'Goblet Squat', 'reps': 16, 'movement_quality': 'Meets criteria'}})
        self.assertEqual(matrix['lower_body'], ESTABLISHED)

    def test_assisted_box_squat_is_beginner(self):
        matrix = evaluate_capability_matrix({'lower_body': {'variation': 'Box squat', 'reps': 6}})
        self.assertEqual(matrix['lower_body'], BEGINNER)

    def test_mid_range_squat_is_developing(self):
        matrix = evaluate_capability_matrix({'lower_body': {'variation': 'Bodyweight squat', 'reps': 11}})
        self.assertEqual(matrix['lower_body'], DEVELOPING)

    def test_floor_pushups_reach_established(self):
        matrix = evaluate_capability_matrix({
            'upper_body': {'pushup_variation': 'Standard Floor Push-up', 'pushup_reps': 14}})
        self.assertEqual(matrix['upper_body'], ESTABLISHED)

    def test_wall_pushups_are_beginner(self):
        matrix = evaluate_capability_matrix({
            'upper_body': {'pushup_variation': 'Wall push-up', 'pushup_reps': 12}})
        self.assertEqual(matrix['upper_body'], BEGINNER)

    def test_core_thresholds(self):
        self.assertEqual(evaluate_capability_matrix({'core': {'hold_seconds': 20}})['core'], BEGINNER)
        self.assertEqual(evaluate_capability_matrix({'core': {'hold_seconds': 45}})['core'], DEVELOPING)
        self.assertEqual(evaluate_capability_matrix({'core': {'hold_seconds': 75}})['core'], ESTABLISHED)

    def test_core_without_a_hold_is_not_assessed(self):
        self.assertEqual(evaluate_capability_matrix({'core': {'variation': 'Plank'}})['core'], NOT_ASSESSED)

    def test_pain_downgrades_to_further_assessment(self):
        """Pain is a safety signal, not a performance score to be averaged away."""
        matrix = evaluate_capability_matrix({
            'lower_body': {'variation': 'Goblet Squat', 'reps': 20,
                           'movement_quality': 'Meets criteria', 'pain_reported': True}})
        self.assertEqual(matrix['lower_body'], FURTHER_ASSESSMENT)


class ValidityTests(unittest.TestCase):

    def test_incomplete_test_contributes_nothing(self):
        matrix = evaluate_capability_matrix({
            'core': {'hold_seconds': 90, 'result_status': 'INCOMPLETE'}})
        self.assertEqual(matrix['core'], NOT_ASSESSED)

    def test_not_assessed_status_contributes_nothing(self):
        matrix = evaluate_capability_matrix({
            'lower_body': {'variation': 'Goblet squat', 'reps': 20, 'result_status': 'NOT_ASSESSED'}})
        self.assertEqual(matrix['lower_body'], NOT_ASSESSED)

    def test_inactive_entries_are_skipped(self):
        matrix = evaluate_capability_matrix({
            'lower_body': {'assessed_tests': [{'variation': 'Goblet squat', 'reps': 20, 'active': False}]}})
        self.assertEqual(matrix['lower_body'], NOT_ASSESSED)


class BalanceTests(unittest.TestCase):

    def test_short_hold_needs_support(self):
        matrix = evaluate_capability_matrix({
            'balance': {'single_leg_left_sec': 9, 'single_leg_right_sec': 20}})
        self.assertEqual(matrix['balance'], NEEDS_SUPPORT)

    def test_steady_hold_is_independent_under_test(self):
        matrix = evaluate_capability_matrix({
            'balance': {'single_leg_left_sec': 25, 'single_leg_right_sec': 22}})
        self.assertEqual(matrix['balance'], INDEPENDENT_UNDER_TEST)

    def test_hands_on_assistance_overrides_duration(self):
        matrix = evaluate_capability_matrix({
            'balance': {'single_leg_left_sec': 40, 'single_leg_right_sec': 40,
                        'support_used': 'Hands-on assistance'}})
        self.assertEqual(matrix['balance'], NEEDS_SUPPORT)

    def test_no_timings_is_not_assessed(self):
        self.assertEqual(evaluate_capability_matrix({'balance': {'notes': 'n/a'}})['balance'], NOT_ASSESSED)


class MobilityTests(unittest.TestCase):

    def test_clean_screens_are_within_protocol(self):
        matrix = evaluate_capability_matrix({'mobility': {
            'shoulder_mobility': 'Comfortable', 'overhead_reach': 'Comfortable',
            'squat_pattern': 'Comfortable'}})
        self.assertEqual(matrix['mobility'], WITHIN_PROTOCOL)

    def test_any_modified_screen_downgrades(self):
        matrix = evaluate_capability_matrix({'mobility': {
            'shoulder_mobility': 'Comfortable', 'overhead_reach': 'Modified'}})
        self.assertEqual(matrix['mobility'], MODIFIED)

    def test_restriction_requires_further_assessment(self):
        matrix = evaluate_capability_matrix({'mobility': {'ankle_knee_to_wall': 'Restricted'}})
        self.assertEqual(matrix['mobility'], FURTHER_ASSESSMENT)

    def test_side_to_side_difference_is_flagged(self):
        """Each side can pass on its own while the asymmetry still matters."""
        matrix = evaluate_capability_matrix({'mobility': {
            'ankle_left_cm': 11.0, 'ankle_right_cm': 5.0}})
        self.assertEqual(matrix['mobility'], MODIFIED)

    def test_small_difference_stays_within_protocol(self):
        matrix = evaluate_capability_matrix({'mobility': {
            'ankle_left_cm': 10.0, 'ankle_right_cm': 8.5}})
        self.assertEqual(matrix['mobility'], WITHIN_PROTOCOL)

    def test_empty_mobility_section_is_not_assessed(self):
        self.assertEqual(evaluate_capability_matrix({'mobility': {}})['mobility'], NOT_ASSESSED)


class CardiovascularTests(unittest.TestCase):

    def test_label_names_the_protocol(self):
        """A cardio result is meaningless without the protocol that produced it."""
        matrix = evaluate_capability_matrix({'cardiovascular': {
            'protocol': '2-minute step test', 'repetitions': 82, 'perceived_exertion': 6}})
        self.assertIn('2-minute step test', matrix['cardiovascular'])

    def test_modified_protocol_is_labelled(self):
        matrix = evaluate_capability_matrix({'cardiovascular': {
            'protocol': '6-minute walk', 'distance_m': 380, 'result_status': 'MODIFIED'}})
        self.assertIn('Modified', matrix['cardiovascular'])

    def test_protocol_without_result_is_not_assessed(self):
        self.assertEqual(evaluate_capability_matrix({'cardiovascular': {}})['cardiovascular'], NOT_ASSESSED)

    def test_symptoms_require_further_assessment(self):
        matrix = evaluate_capability_matrix({'cardiovascular': {
            'protocol': 'Treadmill walk', 'duration_minutes': 4, 'symptoms': 'chest tightness'}})
        self.assertEqual(matrix['cardiovascular'], FURTHER_ASSESSMENT)

    def test_cardio_alias_key_is_accepted(self):
        matrix = evaluate_capability_matrix({'cardio': {
            'protocol': 'Stationary cycle', 'duration_minutes': 6}})
        self.assertIn('Stationary cycle', matrix['cardiovascular'])


class FunctionalMovementTests(unittest.TestCase):

    def test_clean_warm_up_is_independent(self):
        matrix = evaluate_capability_matrix({'warm_up_observation': {
            'walking': 'Normal', 'sit_to_stand': 'Independent', 'squat_pattern': 'Comfortable'}})
        self.assertEqual(matrix['functional_movement'], INDEPENDENT)

    def test_support_means_assistance_needed(self):
        matrix = evaluate_capability_matrix({'warm_up_observation': {
            'walking': 'Normal', 'sit_to_stand': 'Uses support'}})
        self.assertEqual(matrix['functional_movement'], ASSISTANCE_NEEDED)

    def test_cueing_is_modified(self):
        matrix = evaluate_capability_matrix({'warm_up_observation': {
            'walking': 'Normal', 'step_up': 'Needs cueing'}})
        self.assertEqual(matrix['functional_movement'], MODIFIED)

    def test_pain_requires_further_assessment(self):
        matrix = evaluate_capability_matrix({'warm_up_observation': {
            'squat_pattern': 'Pain reported'}})
        self.assertEqual(matrix['functional_movement'], FURTHER_ASSESSMENT)

    def test_empty_observation_is_not_assessed(self):
        self.assertEqual(
            evaluate_capability_matrix({'warm_up_observation': {}})['functional_movement'], NOT_ASSESSED)


class FullBatteryTests(unittest.TestCase):

    def test_a_complete_battery_assesses_all_seven_domains(self):
        matrix = evaluate_capability_matrix({
            'warm_up_observation': {'walking': 'Normal', 'sit_to_stand': 'Independent'},
            'lower_body': {'variation': 'Goblet Squat', 'reps': 16, 'movement_quality': 'Meets criteria'},
            'upper_body': {'pushup_variation': 'Standard Floor Push-up', 'pushup_reps': 13},
            'core': {'hold_seconds': 50},
            'balance': {'single_leg_left_sec': 22, 'single_leg_right_sec': 24},
            'mobility': {'shoulder_mobility': 'Comfortable', 'overhead_reach': 'Comfortable'},
            'cardiovascular': {'protocol': '2-minute step test', 'repetitions': 80},
        })
        self.assertEqual(assessed_domain_count(matrix), 7)
        for domain in DOMAINS:
            self.assertNotEqual(matrix[domain], NOT_ASSESSED, domain)

    def test_partial_battery_assesses_only_what_was_logged(self):
        matrix = evaluate_capability_matrix({
            'lower_body': {'variation': 'Bodyweight squat', 'reps': 12},
            'core': {'hold_seconds': 40},
        })
        self.assertEqual(assessed_domain_count(matrix), 2)
        self.assertEqual(matrix['balance'], NOT_ASSESSED)
        self.assertEqual(matrix['mobility'], NOT_ASSESSED)


if __name__ == '__main__':
    unittest.main()
