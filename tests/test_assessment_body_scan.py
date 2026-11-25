"""
Tests for the assessment body-scan geometry.

These cover the maths and the refusal paths, both of which are pure functions — no provider
call, no database. The point of most of them is that the module declines to produce a number
it cannot support: missing joints, an unusable capture, or a girth estimate with no height to
anchor the scale must all come back empty rather than plausible.
"""

import math
import unittest

from apps.tenant_core.services_body_scan import (
    BodyScanNotConfigured,
    FalBodyScanClient,
    analyse_frontal_view,
    analyse_profile_view,
    assess_capture_quality,
    compute_posture_score,
    estimate_circumferences,
    resolve_joint_index,
)

# A minimal COCO-ish name list in the shape the provider returns.
NAMES = [
    'nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear',
    'left_shoulder', 'right_shoulder', 'left_elbow', 'right_elbow',
    'left_wrist', 'right_wrist', 'left_hip', 'right_hip',
    'left_knee', 'right_knee', 'left_ankle', 'right_ankle',
]


def frontal_keypoints(l_sh, r_sh, l_hip, r_hip, l_ank, r_ank):
    kp = [[0, 0]] * len(NAMES)
    kp[5], kp[6] = l_sh, r_sh
    kp[11], kp[12] = l_hip, r_hip
    kp[15], kp[16] = l_ank, r_ank
    return kp


def profile_keypoints(ear, shoulder, hip, ankle):
    kp = [[0, 0]] * len(NAMES)
    kp[3], kp[5], kp[11], kp[15] = ear, shoulder, hip, ankle
    return kp


class JointResolutionTests(unittest.TestCase):
    """Joints are matched by the provider's own names, never by hardcoded index."""

    def test_resolves_by_name(self):
        self.assertEqual(resolve_joint_index(NAMES, 'left', 'shoulder'), 5)
        self.assertEqual(resolve_joint_index(NAMES, 'right', 'hip'), 12)

    def test_unknown_joint_returns_none(self):
        self.assertIsNone(resolve_joint_index(NAMES, 'left', 'tail'))

    def test_empty_name_list_returns_none(self):
        self.assertIsNone(resolve_joint_index([], 'left', 'hip'))

    def test_prefers_most_specific_match(self):
        names = ['left_shoulder_inner', 'left_shoulder']
        self.assertEqual(resolve_joint_index(names, 'left', 'shoulder'), 1)


class FrontalGeometryTests(unittest.TestCase):

    def test_level_subject_reads_zero(self):
        kp = frontal_keypoints([400, 300], [600, 300], [430, 700], [570, 700], [440, 1200], [560, 1200])
        out = analyse_frontal_view(NAMES, kp)
        self.assertAlmostEqual(out['shoulder_tilt_degrees'], 0.0, places=2)
        self.assertAlmostEqual(out['hip_tilt_degrees'], 0.0, places=2)
        self.assertAlmostEqual(out['lateral_shift_degrees'], 0.0, places=2)

    def test_known_ten_degree_tilt_is_recovered(self):
        drop = 200 * math.tan(math.radians(10))
        kp = frontal_keypoints([400, 300], [600, 300 + drop], [430, 700], [570, 700], [440, 1200], [560, 1200])
        out = analyse_frontal_view(NAMES, kp)
        self.assertAlmostEqual(out['shoulder_tilt_degrees'], 10.0, places=1)

    def test_positive_means_left_side_higher(self):
        drop = 200 * math.tan(math.radians(10))
        kp = frontal_keypoints([400, 300], [600, 300 + drop], [430, 700], [570, 700], [440, 1200], [560, 1200])
        self.assertGreater(analyse_frontal_view(NAMES, kp)['shoulder_tilt_degrees'], 0)

    def test_angles_are_scale_invariant(self):
        """The same pose shot from twice the distance must report the same angle."""
        drop = 200 * math.tan(math.radians(10))
        near = analyse_frontal_view(NAMES, frontal_keypoints(
            [400, 300], [600, 300 + drop], [430, 700], [570, 700], [440, 1200], [560, 1200]))
        far = analyse_frontal_view(NAMES, frontal_keypoints(
            [800, 600], [1200, 600 + 2 * drop], [860, 1400], [1140, 1400], [880, 2400], [1120, 2400]))
        self.assertAlmostEqual(near['shoulder_tilt_degrees'], far['shoulder_tilt_degrees'], places=2)

    def test_mirrored_front_view_reports_the_same_tilt(self):
        """
        Joints are named anatomically, so the subject's left shoulder appears on the RIGHT of
        a front-facing frame and on the LEFT of a back-facing one. The same 10-degree drop
        must read the same either way. Before this was handled, one of the two orientations
        reported 170 degrees instead of 10.
        """
        drop = 200 * math.tan(math.radians(10))
        # Back view: subject's left is on the image left.
        back = analyse_frontal_view(NAMES, frontal_keypoints(
            [400, 300], [600, 300 + drop], [430, 700], [570, 700], [440, 1200], [560, 1200]))
        # Front view: subject's left is on the image right, same body, same drop.
        front = analyse_frontal_view(NAMES, frontal_keypoints(
            [600, 300], [400, 300 + drop], [570, 700], [430, 700], [560, 1200], [440, 1200]))
        self.assertAlmostEqual(back['shoulder_tilt_degrees'], 10.0, places=1)
        self.assertAlmostEqual(front['shoulder_tilt_degrees'], 10.0, places=1)

    def test_tilt_never_exceeds_ninety_degrees(self):
        """A tilt is a deviation from horizontal; 176 degrees is a sign error, not a posture."""
        for layout in (
            frontal_keypoints([400, 300], [600, 340], [430, 700], [570, 700], [440, 1200], [560, 1200]),
            frontal_keypoints([600, 300], [400, 340], [570, 700], [430, 700], [560, 1200], [440, 1200]),
        ):
            out = analyse_frontal_view(NAMES, layout)
            for key in ('shoulder_tilt_degrees', 'hip_tilt_degrees'):
                if out.get(key) is not None:
                    self.assertLessEqual(abs(out[key]), 90.0)

    def test_missing_joints_yield_no_metric_rather_than_zero(self):
        out = analyse_frontal_view(['nose'] * len(NAMES), [[0, 0]] * len(NAMES))
        self.assertNotIn('shoulder_tilt_degrees', out)
        self.assertNotIn('hip_tilt_degrees', out)


class ProfileGeometryTests(unittest.TestCase):

    def test_forward_head_angle_is_recovered(self):
        offset = 100 * math.tan(math.radians(15))
        kp = profile_keypoints([500 + offset, 400], [500, 500], [500, 900], [500, 1400])
        out = analyse_profile_view(NAMES, kp, 'left')
        self.assertAlmostEqual(out['forward_head_angle_degrees'], 15.0, places=1)

    def test_neutral_head_reads_zero(self):
        kp = profile_keypoints([500, 400], [500, 500], [500, 900], [500, 1400])
        self.assertAlmostEqual(analyse_profile_view(NAMES, kp, 'left')['forward_head_angle_degrees'], 0.0, places=2)


class PostureScoreTests(unittest.TestCase):

    def test_no_measured_angle_means_no_score(self):
        self.assertIsNone(compute_posture_score(None, None, None, None))

    def test_perfect_alignment_scores_100(self):
        self.assertEqual(compute_posture_score(0.0, 0.0, 0.0, 0.0)['score'], 100.0)

    def test_poor_alignment_is_penalised(self):
        self.assertLess(compute_posture_score(12.0, 9.0, 28.0, 10.0)['score'], 60.0)

    def test_score_carries_its_components(self):
        """The specification only permits a composite score when its weighting is shown."""
        result = compute_posture_score(12.0, 9.0, 28.0, 10.0)
        self.assertEqual(len(result['components']), 4)
        for component in result['components']:
            self.assertIn('penalty', component)
            self.assertIn('tolerance_degrees', component)

    def test_unmeasured_components_are_marked_not_assessed(self):
        result = compute_posture_score(3.0, None, None, None)
        statuses = [c['status'] for c in result['components']]
        self.assertEqual(statuses.count('not_assessed'), 3)

    def test_score_never_leaves_range(self):
        result = compute_posture_score(90.0, 90.0, 90.0, 90.0)
        self.assertGreaterEqual(result['score'], 0.0)
        self.assertLessEqual(result['score'], 100.0)


class CircumferenceEstimateTests(unittest.TestCase):

    def setUp(self):
        self.frontal = analyse_frontal_view(NAMES, frontal_keypoints(
            [400, 300], [600, 300], [430, 700], [570, 700], [440, 1200], [560, 1200]))
        self.profile = analyse_profile_view(NAMES, profile_keypoints(
            [500, 400], [500, 500], [500, 900], [500, 1400]), 'left')

    def test_no_height_means_no_estimate(self):
        out = estimate_circumferences(self.frontal, self.profile, None)
        self.assertEqual(out['values'], {})
        self.assertIn('unavailable_reason', out)

    def test_estimates_are_tagged_low_confidence(self):
        out = estimate_circumferences(self.frontal, self.profile, 170.0)
        self.assertTrue(out['values'])
        self.assertEqual(out['source'], 'estimated')
        self.assertEqual(out['confidence'], 'low')

    def test_estimates_document_their_assumptions(self):
        out = estimate_circumferences(self.frontal, self.profile, 170.0)
        self.assertGreaterEqual(len(out['assumptions']), 4)
        self.assertIn('method', out)


class CaptureQualityTests(unittest.TestCase):

    def test_no_person_detected_is_unusable(self):
        verdict = assess_capture_quality({'metadata': {'num_people': 0, 'people': []}})
        self.assertFalse(verdict['usable'])
        self.assertIn('reason', verdict)

    def test_multiple_people_is_unusable(self):
        verdict = assess_capture_quality({'metadata': {
            'num_people': 2,
            'people': [{'keypoints_2d': [[1, 1]] * 70}, {'keypoints_2d': [[1, 1]] * 70}],
        }})
        self.assertFalse(verdict['usable'])

    def test_subject_too_small_in_frame_is_unusable(self):
        verdict = assess_capture_quality(
            {'metadata': {'num_people': 1, 'people': [{'bbox': [0, 0, 100, 200], 'keypoints_2d': [[1, 1]] * 70}]}},
            image_size=(1000, 1000),
        )
        self.assertFalse(verdict['usable'])

    def test_well_framed_subject_is_usable(self):
        verdict = assess_capture_quality(
            {'metadata': {'num_people': 1, 'people': [{'bbox': [100, 50, 400, 950], 'keypoints_2d': [[1, 1]] * 70}]}},
            image_size=(1000, 1000),
        )
        self.assertTrue(verdict['usable'])

    def test_too_few_keypoints_is_unusable(self):
        verdict = assess_capture_quality(
            {'metadata': {'num_people': 1, 'people': [{'bbox': [100, 50, 400, 950], 'keypoints_2d': [[1, 1]] * 3}]}},
            image_size=(1000, 1000),
        )
        self.assertFalse(verdict['usable'])


class ClientConfigurationTests(unittest.TestCase):

    def test_missing_key_is_not_configured(self):
        self.assertFalse(FalBodyScanClient(api_key='').is_configured)

    def test_unconfigured_client_refuses_to_run(self):
        with self.assertRaises(BodyScanNotConfigured):
            FalBodyScanClient(api_key='').analyse_image(b'not-an-image')


if __name__ == '__main__':
    unittest.main()


class ProviderCallReuseTests(unittest.TestCase):
    """
    Retaking one view must not re-bill the other three.

    Every provider call costs money, so a view whose bytes are unchanged and whose last
    verdict was usable is reused rather than sent again.
    """

    class _CountingClient:
        provider_name = 'test'
        model = 'test/model'
        is_configured = True

        def __init__(self):
            self.calls = []

        def analyse_image(self, image_bytes, content_type='image/jpeg'):
            self.calls.append(image_bytes)
            return {'metadata': {
                'num_people': 1,
                'keypoint_names': NAMES,
                'people': [{'bbox': [100, 50, 400, 950], 'keypoints_2d': frontal_keypoints(
                    [400, 300], [600, 300], [430, 700], [570, 700], [440, 1200], [560, 1200])}],
            }}

    class _StubFile:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __bool__(self):
            return True

        def open(self, mode='rb'):
            return self

        def read(self):
            return self.payload

        def close(self):
            pass

    class _StubAssessment:
        id = 'stub'
        measured_height_cm = 170
        scan_raw_payload: dict = {}
        scan_capture_quality: dict = {}

        def __init__(self, front, back):
            self.photo_front = front
            self.photo_back = back
            self.photo_side_left = None
            self.photo_side_right = None
            self.photo_side = None

    def _run(self, assessment, client):
        from apps.tenant_core.services_body_scan import BodyScanService
        result = BodyScanService.analyse(assessment, client=client)
        # Mimic what apply_to_assessment persists, so the next run can match digests.
        assessment.scan_capture_quality = result['quality']
        assessment.scan_raw_payload = {
            'views_submitted': result['views_submitted'],
            'views_usable': result['views_usable'],
            'per_view': result['per_view'],
            'raw': result['raw'],
        }
        return result

    def test_unchanged_views_are_not_sent_again(self):
        front = self._StubFile(b'front-image-bytes')
        back = self._StubFile(b'back-image-bytes')
        assessment = self._StubAssessment(front, back)
        client = self._CountingClient()

        self._run(assessment, client)
        self.assertEqual(len(client.calls), 2)

        # Coach retakes only the back view; the front bytes are untouched.
        assessment.photo_back = self._StubFile(b'back-image-RETAKEN')
        self._run(assessment, client)

        self.assertEqual(len(client.calls), 3, 'only the retaken view should be sent again')
        self.assertEqual(client.calls[2], b'back-image-RETAKEN')

    def test_a_reused_view_is_marked_as_such(self):
        assessment = self._StubAssessment(self._StubFile(b'f'), self._StubFile(b'b'))
        client = self._CountingClient()
        self._run(assessment, client)
        result = self._run(assessment, client)
        self.assertTrue(result['raw']['front'].get('reused'))

    def test_a_failed_view_is_retried_rather_than_reused(self):
        """A rejected capture is sent again: the coach may have fixed lighting, not framing."""
        assessment = self._StubAssessment(self._StubFile(b'f'), self._StubFile(b'b'))
        client = self._CountingClient()
        self._run(assessment, client)
        # Mark the front view as having failed last time.
        assessment.scan_capture_quality['front'] = {'usable': False, 'reason': 'too dark'}
        before = len(client.calls)
        self._run(assessment, client)
        self.assertEqual(len(client.calls), before + 1)
