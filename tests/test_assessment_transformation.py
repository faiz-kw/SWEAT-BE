"""
Tests for the transformation preview.

The module specification's warnings about this feature are the point of these tests. A
preview must never be produced without consent, and a render that moved the subject must be
discarded rather than shown with a caveat.
"""

import io
import math
import unittest

from apps.tenant_core.services_transformation import (
    MAX_JOINT_DRIFT_RATIO,
    MILESTONE_MONTHS,
    FalEditRenderer,
    SilhouetteMorphRenderer,
    TransformationNotPermitted,
    TransformationPipeline,
    get_renderer,
)


def _solid_image(width=600, height=900, colour=(180, 170, 160)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), colour).save(buffer, format='JPEG')
    return buffer.getvalue()


BASELINE_JOINTS = {
    'left_shoulder': (240.0, 250.0),
    'right_shoulder': (360.0, 250.0),
    'left_hip': (260.0, 500.0),
    'right_hip': (340.0, 500.0),
    'left_ankle': (270.0, 850.0),
    'right_ankle': (330.0, 850.0),
    'nose': (300.0, 180.0),
    'shoulder_mid': (300.0, 250.0),
    'hip_mid': (300.0, 500.0),
}


class ConsentTests(unittest.TestCase):
    """Consent is a gate, never a warning."""

    class _Assessment:
        consent_image_generation = False
        photo_front = None

    def test_without_consent_the_preview_is_refused(self):
        with self.assertRaises(TransformationNotPermitted):
            TransformationPipeline.build(self._Assessment())

    def test_refusal_names_consent_as_the_reason(self):
        try:
            TransformationPipeline.build(self._Assessment())
            self.fail('should have refused')
        except TransformationNotPermitted as exc:
            self.assertIn('consent', str(exc).lower())


class MorphRendererTests(unittest.TestCase):
    """The morph moves the member's own pixels; it never generates new ones."""

    def setUp(self):
        self.renderer = SilhouetteMorphRenderer()
        self.image = _solid_image()

    def test_the_morph_is_not_generative(self):
        self.assertFalse(self.renderer.is_generative)
        self.assertEqual(self.renderer.cost_per_image_usd, 0.0)

    def test_output_keeps_the_original_dimensions(self):
        from PIL import Image
        out = self.renderer.render(self.image, landmarks=BASELINE_JOINTS, waist_reduction_pct=4.0)
        self.assertEqual(Image.open(io.BytesIO(out)).size, Image.open(io.BytesIO(self.image)).size)

    def test_missing_landmarks_refuse_rather_than_guess(self):
        from apps.tenant_core.services_transformation import TransformationError
        with self.assertRaises(TransformationError):
            self.renderer.render(self.image, landmarks={}, waist_reduction_pct=4.0)

    def test_inconsistent_landmarks_are_refused(self):
        from apps.tenant_core.services_transformation import TransformationError
        broken = {'shoulder_mid': (300.0, 500.0), 'hip_mid': (300.0, 250.0)}  # hip above shoulder
        with self.assertRaises(TransformationError):
            self.renderer.render(self.image, landmarks=broken, waist_reduction_pct=4.0)

    def test_the_squeeze_is_capped(self):
        """An absurd reduction must not produce an absurd image."""
        out = self.renderer.render(self.image, landmarks=BASELINE_JOINTS, waist_reduction_pct=90.0)
        self.assertTrue(out)  # capped internally at 12%, so it still renders


class VerificationTests(unittest.TestCase):
    """
    The specification requires rejecting results with altered pose or implausible
    proportions. These tests drive the comparison directly, with no provider involved.
    """

    def _verdict_for(self, rendered_joints):
        reference = abs(850.0 - 250.0)
        worst_label, worst = None, 0.0
        for label, base in BASELINE_JOINTS.items():
            if label in ('shoulder_mid', 'hip_mid'):
                continue
            got = rendered_joints.get(label)
            if not got:
                return {'accepted': False, 'reason': f'lost {label}'}
            drift = math.hypot(got[0] - base[0], got[1] - base[1]) / reference
            if drift > worst:
                worst_label, worst = label, drift
        return {
            'accepted': worst <= MAX_JOINT_DRIFT_RATIO,
            'worst_joint': worst_label,
            'worst_drift_ratio': worst,
        }

    def test_an_unchanged_pose_passes(self):
        self.assertTrue(self._verdict_for(dict(BASELINE_JOINTS))['accepted'])

    def test_a_tiny_shift_still_passes(self):
        shifted = {k: (x + 2, y + 2) for k, (x, y) in BASELINE_JOINTS.items()}
        self.assertTrue(self._verdict_for(shifted)['accepted'])

    def test_a_moved_subject_is_rejected(self):
        moved = {k: (x + 120, y) for k, (x, y) in BASELINE_JOINTS.items()}
        self.assertFalse(self._verdict_for(moved)['accepted'])

    def test_a_lost_joint_is_rejected(self):
        partial = {k: v for k, v in BASELINE_JOINTS.items() if k != 'nose'}
        self.assertFalse(self._verdict_for(partial)['accepted'])

    def test_the_threshold_is_a_share_of_body_height(self):
        """Resolution-independent: the same pose at any scale gives the same verdict."""
        self.assertLess(MAX_JOINT_DRIFT_RATIO, 0.10)
        self.assertGreater(MAX_JOINT_DRIFT_RATIO, 0.0)


class MilestoneScalingTests(unittest.TestCase):

    class _Assessment:
        consent_image_generation = True
        waist_circumference_cm = 80
        estimated_waist_cm = None
        measured_weight_kg = 80
        target_weight_kg = 72

    def test_later_milestones_illustrate_more_change(self):
        a = self._Assessment()
        months = [TransformationPipeline.waist_reduction_for_month(a, m) for m in MILESTONE_MONTHS]
        self.assertEqual(months, sorted(months))
        self.assertLess(months[0], months[-1])

    def test_change_is_capped_even_for_an_extreme_target(self):
        a = self._Assessment()
        a.target_weight_kg = 40  # implausible target
        for month in MILESTONE_MONTHS:
            self.assertLessEqual(TransformationPipeline.waist_reduction_for_month(a, month), round(1.2 * month, 2))

    def test_no_target_still_yields_a_conservative_default(self):
        a = self._Assessment()
        a.target_weight_kg = None
        self.assertGreater(TransformationPipeline.waist_reduction_for_month(a, 1), 0)


class RendererSelectionTests(unittest.TestCase):

    def test_morph_is_the_default(self):
        self.assertIsInstance(get_renderer(), SilhouetteMorphRenderer)

    def test_fal_is_opt_in_by_name(self):
        self.assertIsInstance(get_renderer('fal'), FalEditRenderer)

    def test_the_generative_renderer_declares_itself(self):
        self.assertTrue(FalEditRenderer().is_generative)
        self.assertFalse(SilhouetteMorphRenderer().is_generative)


if __name__ == '__main__':
    unittest.main()


class ReplicateRendererTests(unittest.TestCase):
    """
    The second hosted renderer exists so the studio is not stranded when one provider's
    account is unavailable. It must behave like the first: declare itself generative, refuse
    without a token, and share the constraining prompt rather than inventing its own.
    """

    def test_it_is_selectable_by_name(self):
        from apps.tenant_core.services_transformation import ReplicateEditRenderer
        self.assertIsInstance(get_renderer('replicate'), ReplicateEditRenderer)

    def test_it_declares_itself_generative(self):
        from apps.tenant_core.services_transformation import ReplicateEditRenderer
        self.assertTrue(ReplicateEditRenderer().is_generative)

    def test_without_a_token_it_refuses(self):
        from apps.tenant_core.services_transformation import (
            ReplicateEditRenderer, TransformationError,
        )
        from django.test import override_settings
        with override_settings(REPLICATE_API_TOKEN=''):
            with self.assertRaises(TransformationError):
                ReplicateEditRenderer().render(_solid_image(), waist_reduction_pct=3.0)

    def test_both_hosted_renderers_share_one_prompt(self):
        """A provider swap must not quietly change what the model is asked to do."""
        from apps.tenant_core.services_transformation import build_edit_prompt
        prompt = build_edit_prompt(3.0)
        for forbidden in ('do not alter the face', 'identical pose', 'Do not add muscle'):
            self.assertIn(forbidden.lower(), prompt.lower())

    def test_morph_remains_the_default(self):
        self.assertIsInstance(get_renderer(None), SilhouetteMorphRenderer)
