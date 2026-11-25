"""
apps/tenant_core/services_transformation.py — the transformation preview pipeline.

The module specification is unusually specific about how this must work, because it is the
easiest part of the product to get wrong:

    "Do not ask a general-purpose image generator to create a 'better version' of the person
     from scratch. That is the easiest way to change their face, body proportions, pose,
     clothing and skin texture."

It then prescribes four steps, which this module implements in order:

  1. Lock the reference   — store the baseline capture and its pose landmarks, immutably.
  2. Lock identity and pose — carry those landmarks through, and verify them afterwards.
  3. Apply constrained changes — body-shape editing guided by plausible circumference change,
                                 not free generation.
  4. Show an honest comparison — "Starting point" beside "Illustrative goal", same framing,
                                 disclosed as a simulation.

Two renderers satisfy step 3:

  * `SilhouetteMorphRenderer` warps the member's own pixels. Identity is preserved by
    construction: the face is never regenerated, so it cannot drift. Free, CPU-only.
  * `FalEditRenderer` sends the capture to a hosted image editor. Better looking, costs per
    image, and the output is not trustworthy on its own — which is what step 4 below is for.

Whichever renders, the result goes through `validate_output`, which re-runs pose detection on
the generated image and rejects it if the joints moved. That is the specification's
validation rule ("reject or flag results with warped limbs, altered pose ... implausible
proportions") and it is the reason a generative renderer is safe to offer at all.

Nothing here runs without `consent_image_generation`.
"""

import io
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

# Milestones the studio previews. The specification treats these as illustrations of a
# trajectory, never as a prediction for a date.
MILESTONE_MONTHS = (1, 2, 3)

# A joint that moves more than this share of the subject's height between the baseline and
# the rendered image means the pose was not preserved, and the render is rejected.
MAX_JOINT_DRIFT_RATIO = 0.04

# Waist change we are willing to illustrate per month, as a share of the starting waist.
# Deliberately conservative: an illustration that outruns what training plausibly delivers
# is the thing the specification is warning about.
WAIST_CHANGE_PCT_PER_MONTH = 1.2


class TransformationError(Exception):
    """Raised when a preview cannot be produced."""


class TransformationNotPermitted(TransformationError):
    """Raised when consent is missing. Never downgraded to a warning."""


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

class SilhouetteMorphRenderer:
    """
    Warps the member's own photograph.

    The torso is squeezed horizontally about the body midline, by a factor that peaks at the
    waist and tapers to nothing at the shoulders and hips, so the change reads as a body
    shape rather than a dent. Pixels are moved, never invented, so identity, clothing, skin
    texture and background are all preserved exactly.

    Implemented with Pillow's mesh transform: the image is cut into horizontal strips and
    each strip is mapped to a slightly narrower quad.
    """

    name = 'silhouette_morph_v1'
    is_generative = False
    cost_per_image_usd = 0.0

    def render(
        self,
        image_bytes: bytes,
        *,
        landmarks: Dict[str, Tuple[float, float]],
        waist_reduction_pct: float,
        **_: Any,
    ) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise TransformationError('Pillow is required to render a morph preview.') from exc

        try:
            source = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        except Exception as exc:
            raise TransformationError(f'The baseline capture could not be read: {exc}') from exc

        width, height = source.size

        shoulder = landmarks.get('shoulder_mid')
        hip = landmarks.get('hip_mid')
        if not shoulder or not hip:
            raise TransformationError(
                'The baseline capture has no usable shoulder and hip landmarks, so the torso '
                'cannot be located. Re-run the body scan first.'
            )

        midline_x = (shoulder[0] + hip[0]) / 2.0
        shoulder_y, hip_y = shoulder[1], hip[1]
        if hip_y <= shoulder_y:
            raise TransformationError('The baseline landmarks are inconsistent; cannot morph.')

        # The waist sits around 55% of the way from shoulder to hip.
        waist_y = shoulder_y + (hip_y - shoulder_y) * 0.55
        span = max(1.0, (hip_y - shoulder_y))
        max_squeeze = max(0.0, min(0.12, waist_reduction_pct / 100.0))

        strips = 48
        strip_h = height / strips
        mesh: List[Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int, int, int, int, int]]] = []

        for i in range(strips):
            top = i * strip_h
            bottom = (i + 1) * strip_h
            centre_y = (top + bottom) / 2.0

            # Falls off smoothly either side of the waist and is zero outside the torso.
            distance = abs(centre_y - waist_y) / span
            factor = max(0.0, 1.0 - (distance / 0.75) ** 2) if distance < 0.75 else 0.0
            squeeze = max_squeeze * factor

            # Source quad is pulled inward, so the destination strip shows a narrower body.
            left = midline_x - (midline_x - 0) * (1.0 - squeeze)
            right = midline_x + (width - midline_x) * (1.0 - squeeze)

            mesh.append((
                (0, int(top), width, int(math.ceil(bottom))),
                (
                    int(left), int(top),
                    int(left), int(math.ceil(bottom)),
                    int(right), int(math.ceil(bottom)),
                    int(right), int(top),
                ),
            ))

        rendered = source.transform(source.size, Image.MESH, mesh, Image.BICUBIC)
        buffer = io.BytesIO()
        rendered.save(buffer, format='JPEG', quality=92)
        return buffer.getvalue()


class FalEditRenderer:
    """
    Hosted image editing through fal.

    The instruction is written to constrain rather than invent: it names the single change we
    want and forbids the ones the specification warns about. That is still not a guarantee —
    the model can ignore it — which is why every output goes through `validate_output`.
    """

    name = 'fal_image_edit_v1'
    is_generative = True

    def __init__(self, model: Optional[str] = None):
        self.model = (model or getattr(settings, 'FAL_TRANSFORMATION_MODEL', '')
                      or 'fal-ai/flux-2/flash/edit').strip()
        self.cost_per_image_usd = float(getattr(settings, 'FAL_TRANSFORMATION_COST_USD', 0.005))

    def render(
        self,
        image_bytes: bytes,
        *,
        waist_reduction_pct: float,
        month: int = 1,
        **_: Any,
    ) -> bytes:
        api_key = (getattr(settings, 'FAL_KEY', '') or '').strip()
        if not api_key:
            raise TransformationError('FAL_KEY is not configured on this server.')
        try:
            import fal_client
        except ImportError as exc:  # pragma: no cover
            raise TransformationError("The 'fal-client' package is not installed.") from exc

        import os
        if not os.environ.get('FAL_KEY'):
            os.environ['FAL_KEY'] = api_key

        try:
            image_url = fal_client.upload(image_bytes, 'image/jpeg')
        except Exception as exc:
            raise TransformationError(f'Could not upload the baseline capture: {exc}') from exc

        prompt = (
            f'Subtly reduce waist circumference by approximately {waist_reduction_pct:.0f} percent, '
            'as the same person would look after some months of consistent training. '
            'Keep the face, hair, skin tone and clothing exactly as they are. '
            'Keep the identical pose, camera angle, framing, lighting and background. '
            'Do not add muscle definition, do not change height or limb proportions, '
            'do not alter the face in any way. Photographic, not stylised.'
        )

        try:
            result = fal_client.subscribe(self.model, arguments={
                'image_url': image_url,
                'prompt': prompt,
                'strength': 0.25,  # a light edit; a strong one rewrites the person
            })
        except Exception as exc:
            raise TransformationError(f'The image editor failed: {exc}') from exc

        url = None
        images = (result or {}).get('images')
        if isinstance(images, list) and images:
            url = (images[0] or {}).get('url')
        elif isinstance((result or {}).get('image'), dict):
            url = result['image'].get('url')
        if not url:
            raise TransformationError('The image editor returned no image.')

        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except Exception as exc:
            raise TransformationError(f'Could not download the rendered image: {exc}') from exc


def get_renderer(name: Optional[str] = None):
    """Returns the configured renderer. Morph is the default: free, and identity-safe."""
    choice = (name or getattr(settings, 'TRANSFORMATION_RENDERER', '') or 'morph').strip().lower()
    if choice in ('fal', 'generative', 'flux'):
        return FalEditRenderer()
    return SilhouetteMorphRenderer()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class TransformationPipeline:
    """Builds the milestone previews for one assessment."""

    @staticmethod
    def detect_landmarks(image_bytes: bytes) -> Dict[str, Any]:
        """
        Runs pose detection on an image and returns the named joints.

        Shared by the reference lock and the output validation, so both sides are measured
        the same way.
        """
        from .services_body_scan import (
            assess_capture_quality, get_scan_client, point_at, resolve_joint_index, midpoint,
        )

        client = get_scan_client()
        if not client.is_configured:
            raise TransformationError(
                'No pose provider is configured, so the rendered image cannot be verified. '
                'The preview is refused rather than shown unverified.'
            )
        response = client.analyse_image(image_bytes, content_type='image/jpeg')
        quality = assess_capture_quality(response)
        if not quality.get('usable'):
            return {'usable': False, 'reason': quality.get('reason'), 'joints': {}}

        metadata = response.get('metadata') or {}
        names = metadata.get('keypoint_names') or []
        person = (metadata.get('people') or [{}])[0]
        keypoints = person.get('keypoints_2d') or []

        joints: Dict[str, Tuple[float, float]] = {}
        for label, tokens in (
            ('left_shoulder', ('left', 'shoulder')), ('right_shoulder', ('right', 'shoulder')),
            ('left_hip', ('left', 'hip')), ('right_hip', ('right', 'hip')),
            ('left_ankle', ('left', 'ankle')), ('right_ankle', ('right', 'ankle')),
            ('nose', ('nose',)),
        ):
            point = point_at(keypoints, resolve_joint_index(names, *tokens))
            if point:
                joints[label] = point

        shoulder_mid = midpoint(joints.get('left_shoulder'), joints.get('right_shoulder'))
        hip_mid = midpoint(joints.get('left_hip'), joints.get('right_hip'))
        if shoulder_mid:
            joints['shoulder_mid'] = shoulder_mid
        if hip_mid:
            joints['hip_mid'] = hip_mid

        return {'usable': True, 'joints': joints}

    @classmethod
    def validate_output(
        cls, rendered_bytes: bytes, baseline_joints: Dict[str, Tuple[float, float]],
    ) -> Dict[str, Any]:
        """
        The specification's validation step.

        Re-detects the pose in the rendered image and compares every joint against the
        baseline. A render that moved the subject, changed their proportions or lost them
        entirely is rejected — it is never shown with a caveat.
        """
        verdict: Dict[str, Any] = {'accepted': False, 'checks': {}}

        try:
            detected = cls.detect_landmarks(rendered_bytes)
        except TransformationError as exc:
            verdict['reason'] = str(exc)
            return verdict

        if not detected.get('usable'):
            verdict['reason'] = (
                'No person could be detected in the rendered image, so it cannot be verified '
                f"against the baseline. ({detected.get('reason')})"
            )
            return verdict

        rendered_joints = detected['joints']

        # Scale drift by the subject's own height so the threshold is resolution-independent.
        shoulder = baseline_joints.get('shoulder_mid')
        ankle_l = baseline_joints.get('left_ankle')
        ankle_r = baseline_joints.get('right_ankle')
        reference = None
        if shoulder and (ankle_l or ankle_r):
            ankle_y = max(p[1] for p in (ankle_l, ankle_r) if p)
            reference = abs(ankle_y - shoulder[1])
        if not reference or reference <= 0:
            verdict['reason'] = 'The baseline has no usable height reference for verification.'
            return verdict

        worst_label, worst_drift = None, 0.0
        for label, baseline_point in baseline_joints.items():
            if label in ('shoulder_mid', 'hip_mid'):
                continue
            rendered_point = rendered_joints.get(label)
            if not rendered_point:
                verdict['reason'] = f'The rendered image lost the {label.replace("_", " ")}.'
                return verdict
            drift = math.hypot(rendered_point[0] - baseline_point[0],
                               rendered_point[1] - baseline_point[1]) / reference
            verdict['checks'][label] = round(drift, 4)
            if drift > worst_drift:
                worst_label, worst_drift = label, drift

        verdict['worst_joint'] = worst_label
        verdict['worst_drift_ratio'] = round(worst_drift, 4)
        verdict['threshold'] = MAX_JOINT_DRIFT_RATIO

        if worst_drift > MAX_JOINT_DRIFT_RATIO:
            verdict['reason'] = (
                f'The rendered image moved the {worst_label.replace("_", " ")} by '
                f'{worst_drift:.1%} of body height, above the {MAX_JOINT_DRIFT_RATIO:.0%} limit. '
                'Pose was not preserved, so the preview was discarded.'
            )
            return verdict

        verdict['accepted'] = True
        return verdict

    @classmethod
    def waist_reduction_for_month(cls, assessment, month: int) -> float:
        """
        How much narrower the waist is illustrated at a given month.

        Driven by the member's own target where one exists, and capped by a conservative rate
        either way, so the illustration never outruns what training plausibly delivers.
        """
        cap = WAIST_CHANGE_PCT_PER_MONTH * month

        current = assessment.waist_circumference_cm or assessment.estimated_waist_cm
        target_weight = getattr(assessment, 'target_weight_kg', None)
        current_weight = assessment.measured_weight_kg

        if current and target_weight and current_weight and float(current_weight) > float(target_weight):
            # Waist tends to move roughly in proportion with weight over this range.
            total_pct = (float(current_weight) - float(target_weight)) / float(current_weight) * 100.0
            share = total_pct * (month / float(max(MILESTONE_MONTHS)))
            return round(min(cap, share), 2)

        return round(cap, 2)

    @classmethod
    def build(cls, assessment, *, renderer=None, db_alias: str = 'default') -> Dict[str, Any]:
        """
        Produces the milestone previews, or explains precisely why it would not.
        """
        if not getattr(assessment, 'consent_image_generation', False):
            raise TransformationNotPermitted(
                'The member has not consented to a generated transformation preview.'
            )

        front = assessment.photo_front
        if not front:
            raise TransformationError('There is no front capture to build a preview from.')

        try:
            front.open('rb')
            try:
                baseline_bytes = front.read()
            finally:
                front.close()
        except Exception as exc:
            raise TransformationError(f'The baseline capture could not be read: {exc}') from exc

        renderer = renderer or get_renderer()

        # Step 1 and 2: lock the reference and its landmarks.
        detected = cls.detect_landmarks(baseline_bytes)
        if not detected.get('usable'):
            raise TransformationError(
                f"The baseline capture is not usable for a preview: {detected.get('reason')}"
            )
        baseline_joints = detected['joints']

        result: Dict[str, Any] = {
            'renderer': renderer.name,
            'is_generative': renderer.is_generative,
            'cost_per_image_usd': getattr(renderer, 'cost_per_image_usd', 0.0),
            'milestones': {},
            'rejected': {},
            'is_simulation': True,
            'disclosure': (
                'An illustration of a trajectory, not a prediction and not a photograph of a '
                'future result. Produced from the member\'s own baseline capture with the pose '
                'and identity held fixed, and verified against the baseline before being shown.'
            ),
        }

        for month in MILESTONE_MONTHS:
            reduction = cls.waist_reduction_for_month(assessment, month)
            try:
                rendered = renderer.render(
                    baseline_bytes,
                    landmarks=baseline_joints,
                    waist_reduction_pct=reduction,
                    month=month,
                )
            except TransformationError as exc:
                result['rejected'][f'month_{month}'] = str(exc)
                continue

            # Step 4's precondition: verify before anything is shown.
            verdict = cls.validate_output(rendered, baseline_joints)
            if not verdict.get('accepted'):
                result['rejected'][f'month_{month}'] = verdict.get('reason', 'Verification failed.')
                continue

            stored = cls._store(assessment, month, rendered, db_alias=db_alias)
            result['milestones'][f'month_{month}'] = {
                'url': stored,
                'waist_reduction_pct': reduction,
                'verification': verdict,
            }

        result['accepted_count'] = len(result['milestones'])
        return result

    @staticmethod
    def _store(assessment, month: int, image_bytes: bytes, db_alias: str = 'default') -> str:
        """Saves a rendered milestone and returns its URL."""
        from django.core.files.base import ContentFile

        filename = f'assessment_{assessment.id}_month_{month}.jpg'

        # The model carries URL columns rather than image fields for the milestones, so the
        # bytes go to the same storage the captures use and the URL is recorded.
        from django.core.files.storage import default_storage
        path = default_storage.save(
            f'assessments/transformation/{filename}', ContentFile(image_bytes),
        )
        url = default_storage.url(path)
        setattr(assessment, f'month_{month}_image_url', url)
        return url
