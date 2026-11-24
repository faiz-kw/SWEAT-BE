"""
apps/tenant_core/services_body_scan.py — Standardised body-scan analysis for Member Assessments.

Provider: fal.ai, model `fal-ai/sam-3/3d-body` (Meta SAM 3D Body). The model returns 70 MHR
body keypoints per detected person, in 2D image coordinates and 3D camera space, plus a GLB
mesh. We use the keypoints.

Design rules, taken from the studio's assessment specification:

1.  Every value this module produces is tagged with a source — `measured`, `calculated`,
    `estimated` or `unavailable` — together with the method that produced it. Nothing is
    returned untagged, and nothing is invented to fill a gap.
2.  Posture angles are *scale-invariant*: an angle between two joints does not depend on
    camera distance, focal length or the subject's height. These are reported with normal
    confidence and are the primary output of the scan.
3.  Circumferences are *scale-dependent* and are derived from joint-centre geometry, not from
    a surface measurement of the body. They are therefore always `estimated` with LOW
    confidence, they require a measured height to anchor the scale, and they NEVER overwrite
    a tape measurement. They are stored in separate `estimated_*` fields so a coach can see
    both numbers side by side.
4.  Body-fat percentage is never derived from the scan. A camera cannot establish it, and the
    specification explicitly forbids presenting one.
5.  A view that fails its quality gate produces no numbers at all. It is recorded as unusable
    with a reason the coach can act on ("move further from the camera", "two people in frame").
"""

import io
import logging
import math
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

# Capture views. Front and both profiles drive the measurements; the back view is used for
# left/right symmetry confirmation.
VIEW_FRONT = 'front'
VIEW_BACK = 'back'
VIEW_LEFT = 'left'
VIEW_RIGHT = 'right'
ALL_VIEWS = (VIEW_FRONT, VIEW_BACK, VIEW_LEFT, VIEW_RIGHT)

PROFILE_VIEWS = (VIEW_LEFT, VIEW_RIGHT)
FRONTAL_VIEWS = (VIEW_FRONT, VIEW_BACK)

# Source tags (specification section 3 / section 17).
SOURCE_MEASURED = 'measured'
SOURCE_CALCULATED = 'calculated'
SOURCE_ESTIMATED = 'estimated'
SOURCE_UNAVAILABLE = 'unavailable'

DEFAULT_MODEL = 'fal-ai/sam-3/3d-body'

# BlazePose's 33 landmarks, in the order MediaPipe returns them. Pinned here because the
# `mediapipe.solutions` package that used to expose this enum was removed in mediapipe 1.x,
# and the Tasks API returns a bare list with no names attached.
BLAZEPOSE_LANDMARK_NAMES = [
    'nose',
    'left_eye_inner', 'left_eye', 'left_eye_outer',
    'right_eye_inner', 'right_eye', 'right_eye_outer',
    'left_ear', 'right_ear',
    'mouth_left', 'mouth_right',
    'left_shoulder', 'right_shoulder',
    'left_elbow', 'right_elbow',
    'left_wrist', 'right_wrist',
    'left_pinky', 'right_pinky',
    'left_index', 'right_index',
    'left_thumb', 'right_thumb',
    'left_hip', 'right_hip',
    'left_knee', 'right_knee',
    'left_ankle', 'right_ankle',
    'left_heel', 'right_heel',
    'left_foot_index', 'right_foot_index',
]

# A landmark the model cannot actually see is worse than a missing one, because it still
# carries plausible coordinates. Below this visibility we discard it, and the geometry that
# depended on it reports "not assessed" rather than a guess.
MIN_LANDMARK_VISIBILITY = 0.5

# Minimum share of the frame height the subject's bounding box must cover for the geometry to
# be worth computing. Below this the keypoints are too coarse to resolve a 2-degree tilt.
MIN_SUBJECT_FRAME_COVERAGE = 0.45


class BodyScanError(Exception):
    """Raised when the scan cannot be performed at all (configuration or transport)."""


class BodyScanNotConfigured(BodyScanError):
    """Raised when no provider credentials are configured."""


# ---------------------------------------------------------------------------
# Provider client
# ---------------------------------------------------------------------------

class FalBodyScanClient:
    """
    Thin wrapper over the fal.ai queue client.

    Kept deliberately small: upload the bytes to fal's CDN (so our own media storage never has
    to be public), then run the model and hand the raw response back. All interpretation of the
    response happens in this module, not in the caller.
    """

    provider_name = 'fal.ai'

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout: Optional[int] = None):
        self.api_key = (api_key if api_key is not None else getattr(settings, 'FAL_KEY', '') or '').strip()
        self.model = (model or getattr(settings, 'FAL_BODY_SCAN_MODEL', '') or DEFAULT_MODEL).strip()
        self.timeout = int(timeout or getattr(settings, 'FAL_BODY_SCAN_TIMEOUT_SECONDS', 180))

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _client(self):
        if not self.is_configured:
            raise BodyScanNotConfigured(
                "FAL_KEY is not set. Body scanning stays disabled until the key is configured."
            )
        try:
            import fal_client  # noqa: WPS433 (import is deliberately lazy)
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise BodyScanNotConfigured(
                "The 'fal-client' package is not installed on this server."
            ) from exc

        # fal_client reads FAL_KEY from the environment on first use. Settings may have loaded
        # it from .env without exporting it, so make sure the variable is present.
        import os
        if not os.environ.get('FAL_KEY'):
            os.environ['FAL_KEY'] = self.api_key
        return fal_client

    @staticmethod
    def _explain(exc: Exception) -> str:
        """
        Pulls the provider's own explanation out of an HTTP error.

        fal answers an account problem with a plain 403 whose body carries the actual reason
        ("Exhausted balance. Top up your balance at ..."). Without this the coach — and whoever
        reads the logs — sees a bare status code and has nothing to act on.
        """
        response = getattr(exc, 'response', None)
        if response is not None:
            detail = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    detail = body.get('detail') or body.get('error') or body.get('message')
                    if isinstance(detail, (dict, list)):
                        detail = str(detail)
            except Exception:
                detail = None
            if not detail:
                try:
                    text = (response.text or '').strip()
                    detail = text[:300] if text else None
                except Exception:
                    detail = None
            status = getattr(response, 'status_code', None)
            if detail:
                return f"{detail} (HTTP {status})" if status else str(detail)
        return str(exc)

    def analyse_image(self, image_bytes: bytes, content_type: str = 'image/jpeg') -> Dict[str, Any]:
        """
        Uploads one image and runs the pose model against it.

        Returns the provider's raw response dict. Raises BodyScanError on transport failure so
        the caller can record the view as failed without aborting the other views.
        """
        fal_client = self._client()

        try:
            image_url = fal_client.upload(image_bytes, content_type)
        except Exception as exc:
            raise BodyScanError(
                f"Could not upload the capture to the scan provider: {self._explain(exc)}"
            ) from exc

        try:
            result = fal_client.subscribe(
                self.model,
                arguments={
                    'image_url': image_url,
                    # The GLB mesh and the keypoint-marker spheres are only needed for 3D
                    # visualisation, which we do not render. Skipping them keeps the response
                    # small and the call cheaper.
                    'export_meshes': False,
                    'include_3d_keypoints': False,
                    'include_mhr_params': False,
                },
            )
        except Exception as exc:
            raise BodyScanError(
                f"The scan provider rejected or failed the request: {self._explain(exc)}"
            ) from exc

        if not isinstance(result, dict):
            raise BodyScanError("The scan provider returned an unexpected response shape.")
        return result


class LocalPoseScanClient:
    """
    On-device pose detection with MediaPipe BlazePose. No account, no per-call cost, and the
    member's photographs never leave the server — which is the stronger position for images
    of this kind.

    It emits the same response shape as the fal client so everything downstream (quality
    gate, geometry, scoring, provenance) is identical regardless of which provider ran.

    The trade against a hosted 3D model is honest: BlazePose is a single-person 2D detector,
    so it copes less well with occlusion and crowded frames, and it cannot produce a body
    surface. Since this module only ever consumed 2D joint positions, neither limitation
    affects the numbers we publish.
    """

    provider_name = 'mediapipe'

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = (
            model_path
            or getattr(settings, 'BODY_SCAN_LOCAL_MODEL_PATH', '')
            or ''
        ).strip()
        self.model = f'mediapipe/{Path(self.model_path).stem}' if self.model_path else 'mediapipe/pose_landmarker'

    @property
    def is_configured(self) -> bool:
        return bool(self.model_path and Path(self.model_path).is_file())

    def _landmarker(self):
        if not self.model_path:
            raise BodyScanNotConfigured(
                "BODY_SCAN_LOCAL_MODEL_PATH is not set. Download a MediaPipe pose_landmarker "
                "task file and point the setting at it."
            )
        if not Path(self.model_path).is_file():
            raise BodyScanNotConfigured(
                f"The pose model file was not found at {self.model_path}."
            )
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise BodyScanNotConfigured(
                "The 'mediapipe' package is not installed on this server "
                "(pip install -r requirements-bodyscan.txt)."
            ) from exc

        options = vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=self.model_path),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=2,  # detect a second person so the quality gate can reject the frame
        )
        return vision.PoseLandmarker.create_from_options(options)

    def analyse_image(self, image_bytes: bytes, content_type: str = 'image/jpeg') -> Dict[str, Any]:
        """Runs pose detection locally and returns a fal-shaped response."""
        try:
            import mediapipe as mp
            import numpy as np
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise BodyScanNotConfigured(
                "Local scanning needs mediapipe, numpy and Pillow installed on this server."
            ) from exc

        try:
            with Image.open(io.BytesIO(image_bytes)) as pil_image:
                rgb = pil_image.convert('RGB')
                frame = np.asarray(rgb)
                width, height = rgb.size
        except Exception as exc:
            raise BodyScanError(f"The capture could not be decoded as an image: {exc}") from exc

        try:
            with self._landmarker() as landmarker:
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame)
                detection = landmarker.detect(mp_image)
        except BodyScanNotConfigured:
            raise
        except Exception as exc:
            raise BodyScanError(f"Local pose detection failed: {exc}") from exc

        people = []
        for person_index, landmarks in enumerate(detection.pose_landmarks or []):
            keypoints: List[Any] = []
            visible_points: List[Tuple[float, float]] = []
            for landmark in landmarks:
                visibility = getattr(landmark, 'visibility', 1.0)
                if visibility is not None and visibility < MIN_LANDMARK_VISIBILITY:
                    keypoints.append(None)
                    continue
                point = (float(landmark.x) * width, float(landmark.y) * height)
                keypoints.append([point[0], point[1]])
                visible_points.append(point)

            if not visible_points:
                continue

            xs = [p[0] for p in visible_points]
            ys = [p[1] for p in visible_points]
            people.append({
                'person_id': person_index,
                'bbox': [min(xs), min(ys), max(xs), max(ys)],
                'focal_length': None,
                'keypoints_2d': keypoints,
                'visible_landmark_count': len(visible_points),
            })

        return {
            'metadata': {
                'num_people': len(people),
                'keypoint_names': list(BLAZEPOSE_LANDMARK_NAMES),
                'people': people,
            },
            'visualization': None,
        }


def get_scan_client():
    """
    Returns the configured provider.

    `local` keeps the photographs on this server and costs nothing; `fal` sends them to a
    hosted 3D model. The setting decides, and the choice is recorded on every assessment so a
    later reader knows which one produced the numbers.
    """
    provider = (getattr(settings, 'BODY_SCAN_PROVIDER', '') or 'local').strip().lower()
    if provider in ('fal', 'fal.ai', 'remote'):
        return FalBodyScanClient()
    return LocalPoseScanClient()


# ---------------------------------------------------------------------------
# Keypoint helpers
# ---------------------------------------------------------------------------

def resolve_joint_index(keypoint_names: Sequence[str], *required_tokens: str) -> Optional[int]:
    """
    Finds the index of a joint by matching tokens against the provider's own name list.

    The 70-name MHR ordering is not published in fal's API documentation — it is returned at
    runtime in `metadata.keypoint_names`. Matching by name rather than hardcoding indices means
    a change in the upstream ordering degrades to "joint not found" instead of silently
    reporting the wrong body part's angle.
    """
    if not keypoint_names:
        return None
    tokens = [t.lower() for t in required_tokens]
    best_index = None
    best_len = None
    for idx, raw_name in enumerate(keypoint_names):
        name = str(raw_name or '').lower().replace('-', '_')
        if all(tok in name for tok in tokens):
            # Prefer the most specific (shortest) match, so "left_shoulder" wins over
            # "left_shoulder_inner" when both would satisfy the tokens.
            if best_len is None or len(name) < best_len:
                best_index = idx
                best_len = len(name)
    return best_index


def point_at(keypoints: Sequence[Sequence[float]], index: Optional[int]) -> Optional[Tuple[float, float]]:
    if index is None or index < 0 or index >= len(keypoints):
        return None
    raw = keypoints[index]
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        return (float(raw[0]), float(raw[1]))
    except (TypeError, ValueError):
        return None


def midpoint(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    if not a or not b:
        return None
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def distance(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> Optional[float]:
    if not a or not b:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_from_horizontal(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> Optional[float]:
    """
    Tilt in degrees of the line between two paired joints, relative to horizontal.

    Callers pass (left_joint, right_joint), named anatomically — the subject's own left and
    right. Which one appears on which SIDE of the image depends on where the camera is: in a
    front view the subject's left shoulder sits on the right of the frame, in a back view it
    sits on the left. So the magnitude is taken from the absolute run and rise, and the sign
    comes from which joint is physically higher. A naive atan2 of the raw deltas reports a
    4-degree drop as 176 degrees on exactly half the captures.

    POSITIVE means the subject's left side sits higher; negative means the right side does.
    The result is always within [-90, 90].
    """
    if not a or not b:
        return None
    dx = abs(b[0] - a[0])
    dy = abs(b[1] - a[1])
    if dx < 1e-6 and dy < 1e-6:
        return None
    magnitude = math.degrees(math.atan2(dy, dx))
    # Smaller y is higher in image space.
    sign = 1.0 if a[1] < b[1] else -1.0
    return sign * magnitude


def angle_from_vertical(lower: Optional[Tuple[float, float]], upper: Optional[Tuple[float, float]]) -> Optional[float]:
    """
    Signed angle in degrees of the segment lower→upper away from true vertical.

    Positive means the upper point sits forward/right of the lower point in image space.
    """
    if not lower or not upper:
        return None
    dx = upper[0] - lower[0]
    dy = lower[1] - upper[1]  # upward is positive
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return None
    return math.degrees(math.atan2(dx, dy))


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

def assess_capture_quality(
    response: Dict[str, Any],
    image_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """
    Decides whether a view is good enough to measure from, before any number is computed.

    This is the specification's "run a quality check for loose fabric, occlusion, lighting,
    body framing and pose" gate. What the pose model can actually evidence is framing and
    person count; fabric and lighting are recorded as coach-entered capture conditions
    elsewhere rather than guessed at here.
    """
    metadata = response.get('metadata') or {}
    people = metadata.get('people') or []
    num_people = metadata.get('num_people')
    if num_people is None:
        num_people = len(people)

    verdict: Dict[str, Any] = {'usable': False, 'warnings': [], 'person_count': num_people}

    if not people:
        verdict['reason'] = 'No person was detected in this capture. Check framing and lighting, then retake.'
        return verdict

    if num_people and int(num_people) > 1:
        verdict['reason'] = (
            f"{int(num_people)} people were detected in this capture. "
            "Only the member may be in frame — clear the background and retake."
        )
        return verdict

    person = people[0]
    bbox = person.get('bbox') or []
    if len(bbox) == 4 and image_size:
        try:
            _, y_min, _, y_max = [float(v) for v in bbox]
            frame_h = float(image_size[1] or 0)
            if frame_h > 0:
                coverage = abs(y_max - y_min) / frame_h
                verdict['frame_coverage'] = round(coverage, 3)
                if coverage < MIN_SUBJECT_FRAME_COVERAGE:
                    verdict['reason'] = (
                        'The member occupies too little of the frame for reliable angles '
                        f'({coverage:.0%} of frame height). Move the camera closer or reframe, then retake.'
                    )
                    return verdict
                if coverage > 0.99:
                    verdict['warnings'].append(
                        'The member fills the entire frame and may be cropped. Head and feet must both be visible.'
                    )
        except (TypeError, ValueError):
            pass

    keypoints = person.get('keypoints_2d') or []
    if len(keypoints) < 10:
        verdict['reason'] = 'The pose model could not resolve enough joints in this capture. Retake with the full body visible.'
        return verdict

    verdict['usable'] = True
    return verdict


# ---------------------------------------------------------------------------
# Posture geometry
# ---------------------------------------------------------------------------

def analyse_frontal_view(keypoint_names: Sequence[str], keypoints: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """
    Front or back view: shoulder tilt, hip tilt and lateral midline shift.

    All three are angles, so they hold regardless of how far away the camera was.
    """
    l_sh = point_at(keypoints, resolve_joint_index(keypoint_names, 'left', 'shoulder'))
    r_sh = point_at(keypoints, resolve_joint_index(keypoint_names, 'right', 'shoulder'))
    l_hip = point_at(keypoints, resolve_joint_index(keypoint_names, 'left', 'hip'))
    r_hip = point_at(keypoints, resolve_joint_index(keypoint_names, 'right', 'hip'))
    l_ank = point_at(keypoints, resolve_joint_index(keypoint_names, 'left', 'ankle'))
    r_ank = point_at(keypoints, resolve_joint_index(keypoint_names, 'right', 'ankle'))

    out: Dict[str, Any] = {}

    shoulder_tilt = angle_from_horizontal(l_sh, r_sh)
    if shoulder_tilt is not None:
        out['shoulder_tilt_degrees'] = round(shoulder_tilt, 2)

    hip_tilt = angle_from_horizontal(l_hip, r_hip)
    if hip_tilt is not None:
        out['hip_tilt_degrees'] = round(hip_tilt, 2)

    shoulder_mid = midpoint(l_sh, r_sh)
    ankle_mid = midpoint(l_ank, r_ank)
    lateral = angle_from_vertical(ankle_mid, shoulder_mid)
    if lateral is not None:
        out['lateral_shift_degrees'] = round(lateral, 2)

    # Raw pixel spans, kept so the scale conversion downstream has something to work from.
    shoulder_span = distance(l_sh, r_sh)
    hip_span = distance(l_hip, r_hip)
    stature_span = distance(midpoint(l_sh, r_sh), ankle_mid)
    if shoulder_span:
        out['shoulder_span_px'] = round(shoulder_span, 2)
    if hip_span:
        out['hip_span_px'] = round(hip_span, 2)
    if stature_span:
        out['shoulder_to_ankle_span_px'] = round(stature_span, 2)

    return out


def analyse_profile_view(keypoint_names: Sequence[str], keypoints: Sequence[Sequence[float]], side: str) -> Dict[str, Any]:
    """
    Left or right profile: forward-head angle and torso depth.

    Forward-head angle is measured from the shoulder up to the ear, against vertical — the
    standard craniovertebral construction. We take the joints on the side facing the camera.
    """
    ear = point_at(keypoints, resolve_joint_index(keypoint_names, side, 'ear'))
    shoulder = point_at(keypoints, resolve_joint_index(keypoint_names, side, 'shoulder'))
    hip = point_at(keypoints, resolve_joint_index(keypoint_names, side, 'hip'))
    ankle = point_at(keypoints, resolve_joint_index(keypoint_names, side, 'ankle'))

    out: Dict[str, Any] = {}

    forward_head = angle_from_vertical(shoulder, ear)
    if forward_head is not None:
        out['forward_head_angle_degrees'] = round(abs(forward_head), 2)

    trunk_lean = angle_from_vertical(hip, shoulder)
    if trunk_lean is not None:
        out['trunk_lean_degrees'] = round(trunk_lean, 2)

    torso_depth = distance(shoulder, hip)
    if torso_depth:
        out['torso_depth_px'] = round(torso_depth, 2)

    stature_span = distance(shoulder, ankle)
    if stature_span:
        out['shoulder_to_ankle_span_px'] = round(stature_span, 2)

    return out


def compute_posture_score(
    shoulder_tilt: Optional[float],
    hip_tilt: Optional[float],
    forward_head: Optional[float],
    lateral_shift: Optional[float],
) -> Optional[Dict[str, Any]]:
    """
    Combines the measured angles into a single 0-100 posture score.

    The specification allows a composite score only when its components and weighting are
    shown, so the breakdown travels with the number and the caller stores both. A score is
    returned only when at least one angle was actually measured — there is no default.
    """
    components: List[Dict[str, Any]] = []
    penalty_total = 0.0

    def add(label: str, value: Optional[float], per_degree: float, tolerance: float, cap: float):
        nonlocal penalty_total
        if value is None:
            components.append({'metric': label, 'value_degrees': None, 'penalty': 0.0, 'status': 'not_assessed'})
            return
        magnitude = abs(value)
        over = max(0.0, magnitude - tolerance)
        penalty = min(cap, over * per_degree)
        penalty_total += penalty
        components.append({
            'metric': label,
            'value_degrees': round(magnitude, 2),
            'tolerance_degrees': tolerance,
            'penalty_per_degree_over_tolerance': per_degree,
            'penalty': round(penalty, 2),
            'status': 'within_tolerance' if over <= 0 else 'outside_tolerance',
        })

    add('shoulder_tilt', shoulder_tilt, per_degree=3.0, tolerance=2.0, cap=20.0)
    add('hip_tilt', hip_tilt, per_degree=3.0, tolerance=2.0, cap=20.0)
    add('forward_head_angle', forward_head, per_degree=1.5, tolerance=10.0, cap=25.0)
    add('lateral_shift', lateral_shift, per_degree=2.5, tolerance=2.0, cap=15.0)

    if all(c['status'] == 'not_assessed' for c in components):
        return None

    score = max(0.0, min(100.0, 100.0 - penalty_total))
    return {
        'score': round(score, 2),
        'method': 'sweat_posture_score_v1',
        'scale': '0-100, where 100 means every measured angle sat inside its tolerance band',
        'components': components,
        'interpretation_note': (
            'A screening score describing alignment in the captured standing pose. '
            'It is not a diagnosis and does not grade fitness.'
        ),
    }


def estimate_circumferences(
    frontal: Dict[str, Any],
    profile: Dict[str, Any],
    measured_height_cm: Optional[float],
) -> Dict[str, Any]:
    """
    Estimates girths by treating each cross-section as an ellipse: frontal width gives one
    axis, profile depth the other, with Ramanujan's perimeter approximation.

    This is deliberately labelled LOW confidence. The inputs are joint centres, not the skin
    surface, so the result tracks *change over time* far better than it reproduces a tape.
    It requires a measured standing height to convert pixels into centimetres, and returns
    nothing at all when that height is missing.
    """
    out: Dict[str, Any] = {
        'method': 'ellipse_from_pose_geometry_v1',
        'confidence': 'low',
        'source': SOURCE_ESTIMATED,
        'assumptions': [
            'Cross-sections are approximated as ellipses from frontal width and profile depth.',
            'Axes are derived from joint-centre positions, not from the skin surface, so absolute values read low.',
            'Pixel-to-centimetre scale is anchored on the measured standing height.',
            'Intended for tracking change between assessments under identical capture conditions.',
        ],
        'values': {},
    }

    if not measured_height_cm:
        out['unavailable_reason'] = (
            'No measured standing height on record. Girth estimates need a scale reference, '
            'so none were produced.'
        )
        return out

    stature_px = frontal.get('shoulder_to_ankle_span_px') or profile.get('shoulder_to_ankle_span_px')
    if not stature_px:
        out['unavailable_reason'] = 'The captures did not yield a usable shoulder-to-ankle span to anchor the scale.'
        return out

    # The shoulder-to-ankle joint span covers roughly 0.72 of standing height in adults; the
    # remainder is head above the shoulder line and foot below the ankle joint. Documented here
    # because it is the single largest source of error in the conversion.
    stature_fraction = 0.72
    cm_per_px = (float(measured_height_cm) * stature_fraction) / float(stature_px)
    out['scale_cm_per_px'] = round(cm_per_px, 5)
    out['assumptions'].append(
        f'Shoulder-to-ankle joint span is taken as {stature_fraction:.2f} of standing height.'
    )

    depth_px = profile.get('torso_depth_px')

    def ellipse_perimeter(width_cm: float, depth_cm: float) -> float:
        a = width_cm / 2.0
        b = depth_cm / 2.0
        if a <= 0 or b <= 0:
            return 0.0
        # Ramanujan's second approximation.
        h = ((a - b) ** 2) / ((a + b) ** 2)
        return math.pi * (a + b) * (1.0 + (3.0 * h) / (10.0 + math.sqrt(4.0 - 3.0 * h)))

    for key, span_key, depth_ratio in (
        ('chest_cm', 'shoulder_span_px', 0.62),
        ('waist_cm', 'hip_span_px', 0.72),
        ('hip_cm', 'hip_span_px', 0.80),
    ):
        span_px = frontal.get(span_key)
        if not span_px or not depth_px:
            continue
        width_cm = float(span_px) * cm_per_px
        depth_cm = float(depth_px) * cm_per_px * depth_ratio
        value = ellipse_perimeter(width_cm, depth_cm)
        if value > 0:
            out['values'][key] = round(value, 1)

    if frontal.get('shoulder_span_px'):
        out['values']['shoulder_width_cm'] = round(float(frontal['shoulder_span_px']) * cm_per_px, 1)

    if not out['values']:
        out['unavailable_reason'] = 'Not enough geometry across the front and profile captures to estimate girths.'

    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class BodyScanService:
    """Runs the four-view scan for one assessment and writes the results back."""

    @staticmethod
    def image_size(image_bytes: bytes) -> Optional[Tuple[int, int]]:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image_bytes)) as img:
                return img.size
        except Exception:
            return None

    @classmethod
    def collect_views(cls, assessment) -> Dict[str, Any]:
        """Returns {view: file_field} for every view that currently has an image."""
        candidates = {
            VIEW_FRONT: assessment.photo_front,
            VIEW_BACK: assessment.photo_back,
            VIEW_LEFT: assessment.photo_side_left,
            VIEW_RIGHT: assessment.photo_side_right or assessment.photo_side,
        }
        return {view: f for view, f in candidates.items() if f}

    @classmethod
    def analyse(cls, assessment, client: Optional[FalBodyScanClient] = None) -> Dict[str, Any]:
        """
        Runs every available view through the provider and derives the metrics.

        Returns a result dict; it does not save. `apply_to_assessment` persists it, so the
        analysis is testable without a database write.
        """
        client = client or get_scan_client()
        views = cls.collect_views(assessment)

        result: Dict[str, Any] = {
            'provider': getattr(client, 'provider_name', 'unknown'),
            'model': client.model,
            'views_submitted': sorted(views.keys()),
            'quality': {},
            'per_view': {},
            'raw': {},
            'errors': {},
        }

        if not views:
            result['status'] = 'FAILED'
            result['error'] = 'No capture images are attached to this assessment.'
            return result

        if not client.is_configured:
            result['status'] = 'UNAVAILABLE'
            result['error'] = (
                f"The configured scan provider ({getattr(client, 'provider_name', 'unknown')}) is not "
                'ready on this server. Captures were stored; no measurements were produced.'
            )
            return result

        for view, file_field in views.items():
            try:
                file_field.open('rb')
                try:
                    image_bytes = file_field.read()
                finally:
                    file_field.close()
            except Exception as exc:
                result['errors'][view] = f'Could not read the stored capture: {exc}'
                result['quality'][view] = {'usable': False, 'reason': 'The stored image could not be read.'}
                continue

            if not image_bytes:
                result['errors'][view] = 'The stored capture was empty.'
                result['quality'][view] = {'usable': False, 'reason': 'The stored image was empty.'}
                continue

            content_type = 'image/png' if image_bytes[:8].startswith(b'\x89PNG') else 'image/jpeg'

            try:
                response = client.analyse_image(image_bytes, content_type=content_type)
            except BodyScanError as exc:
                logger.warning('Body scan failed for assessment %s view %s: %s', assessment.id, view, exc)
                result['errors'][view] = str(exc)
                result['quality'][view] = {'usable': False, 'reason': f'The scan provider could not process this view: {exc}'}
                continue

            quality = assess_capture_quality(response, cls.image_size(image_bytes))
            result['quality'][view] = quality

            metadata = response.get('metadata') or {}
            # Keypoints are bulky; keep the derived geometry and the provider's own reference
            # data, not the full per-joint dump for every view.
            result['raw'][view] = {
                'num_people': metadata.get('num_people'),
                'keypoint_names_count': len(metadata.get('keypoint_names') or []),
                'visualization_url': (response.get('visualization') or {}).get('url')
                if isinstance(response.get('visualization'), dict) else None,
            }

            if not quality.get('usable'):
                continue

            people = metadata.get('people') or []
            person = people[0]
            keypoint_names = metadata.get('keypoint_names') or []
            keypoints = person.get('keypoints_2d') or []

            if not keypoint_names:
                result['quality'][view] = {
                    'usable': False,
                    'reason': 'The provider did not return the keypoint name list, so joints could not be identified safely.',
                }
                continue

            if view in FRONTAL_VIEWS:
                result['per_view'][view] = analyse_frontal_view(keypoint_names, keypoints)
            else:
                side = 'left' if view == VIEW_LEFT else 'right'
                result['per_view'][view] = analyse_profile_view(keypoint_names, keypoints, side)
            result['raw'][view]['bbox'] = person.get('bbox')
            result['raw'][view]['focal_length'] = person.get('focal_length')

        cls._derive_summary(assessment, result)
        return result

    @classmethod
    def _derive_summary(cls, assessment, result: Dict[str, Any]) -> None:
        """Folds the per-view geometry into the assessment-level metrics."""
        per_view = result.get('per_view') or {}
        front = per_view.get(VIEW_FRONT) or {}
        back = per_view.get(VIEW_BACK) or {}
        left = per_view.get(VIEW_LEFT) or {}
        right = per_view.get(VIEW_RIGHT) or {}

        def averaged(key: str, sources: Sequence[Dict[str, Any]]) -> Optional[float]:
            values = [s[key] for s in sources if s.get(key) is not None]
            if not values:
                return None
            return round(sum(values) / len(values), 2)

        # The back view sees the same frontal-plane angles mirrored, so its sign is inverted
        # before averaging with the front view.
        back_mirrored = {k: (-v if isinstance(v, (int, float)) and k.endswith('_degrees') else v) for k, v in back.items()}

        summary: Dict[str, Any] = {
            'shoulder_tilt_degrees': averaged('shoulder_tilt_degrees', [front, back_mirrored]),
            'hip_tilt_degrees': averaged('hip_tilt_degrees', [front, back_mirrored]),
            'lateral_shift_degrees': averaged('lateral_shift_degrees', [front, back_mirrored]),
            'forward_head_angle_degrees': averaged('forward_head_angle_degrees', [left, right]),
            'trunk_lean_degrees': averaged('trunk_lean_degrees', [left, right]),
        }

        # Where both profiles were captured, the spread between them indicates how repeatable
        # the capture was. A wide spread means the member rotated between shots.
        if left.get('forward_head_angle_degrees') is not None and right.get('forward_head_angle_degrees') is not None:
            spread = abs(left['forward_head_angle_degrees'] - right['forward_head_angle_degrees'])
            summary['profile_agreement_degrees'] = round(spread, 2)
            if spread > 6.0:
                result.setdefault('warnings', []).append(
                    f'The two profile captures disagree by {spread:.1f}°. Re-shoot both profiles with the same stance '
                    'before treating the forward-head angle as a baseline.'
                )

        posture = compute_posture_score(
            summary.get('shoulder_tilt_degrees'),
            summary.get('hip_tilt_degrees'),
            summary.get('forward_head_angle_degrees'),
            summary.get('lateral_shift_degrees'),
        )
        if posture:
            summary['posture_score'] = posture['score']
            result['posture_score_breakdown'] = posture

        profile_for_scale = left or right
        result['circumference_estimates'] = estimate_circumferences(
            front or back,
            profile_for_scale,
            float(assessment.measured_height_cm) if assessment.measured_height_cm else None,
        )

        result['summary'] = summary

        usable = [v for v, q in (result.get('quality') or {}).items() if q.get('usable')]
        result['views_usable'] = sorted(usable)

        if not usable:
            result['status'] = 'FAILED'
            result.setdefault('error', 'No capture passed the quality gate, so no measurements were produced.')
        elif len(usable) < len(result.get('views_submitted') or []):
            result['status'] = 'PARTIAL'
        else:
            result['status'] = 'COMPLETED'

    @classmethod
    def build_measurement_metadata(cls, assessment, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds the specification's per-metric provenance block: what each number is, where it
        came from, and how confident we are in it.
        """
        summary = result.get('summary') or {}
        circumference = result.get('circumference_estimates') or {}
        now_model = result.get('model')

        def entry(source: str, method: str, confidence: str, **extra) -> Dict[str, Any]:
            payload = {'source': source, 'method': method, 'confidence': confidence}
            payload.update(extra)
            return payload

        metadata: Dict[str, Any] = {}

        for field in ('measured_height_cm', 'measured_weight_kg', 'waist_circumference_cm', 'resting_heart_rate_bpm'):
            if getattr(assessment, field, None) is not None:
                metadata[field] = entry(SOURCE_MEASURED, 'coach_entered_instrument_reading', 'high')

        if assessment.calculated_bmi is not None:
            metadata['calculated_bmi'] = entry(
                SOURCE_CALCULATED, 'weight_kg / height_m^2', 'high',
                note='A screening and context measure. It does not measure body fat or muscularity.',
            )

        if assessment.bmr_rmr_kcal is not None:
            metadata['bmr_rmr_kcal'] = entry(
                SOURCE_ESTIMATED, 'mifflin_st_jeor_equation', 'medium',
                note='Estimated from height, weight, age and sex — not measured by indirect calorimetry.',
            )
        if assessment.tdee_kcal is not None:
            metadata['tdee_kcal'] = entry(
                SOURCE_ESTIMATED, 'rmr_x_activity_multiplier', 'low',
                note='An estimate to start planning from. Calibrate against observed weight trend.',
            )

        for key in ('shoulder_tilt_degrees', 'hip_tilt_degrees', 'forward_head_angle_degrees',
                    'lateral_shift_degrees', 'trunk_lean_degrees'):
            if summary.get(key) is not None:
                metadata[key] = entry(
                    SOURCE_CALCULATED, f'pose_keypoint_geometry ({now_model})', 'medium',
                    note='An angle between detected joints, independent of camera distance.',
                )

        if summary.get('posture_score') is not None:
            metadata['posture_score'] = entry(
                SOURCE_CALCULATED, 'sweat_posture_score_v1', 'medium',
                note='Composite of the measured angles; components stored alongside the score.',
            )

        for key, value in (circumference.get('values') or {}).items():
            metadata[f'estimated_{key}'] = entry(
                SOURCE_ESTIMATED, circumference.get('method', 'ellipse_from_pose_geometry_v1'), 'low',
                note='Derived from joint-centre geometry, not a surface measurement. Not a tape replacement.',
            )

        metadata['estimated_body_fat_pct'] = entry(
            SOURCE_UNAVAILABLE, 'not_derivable_from_capture', 'none',
            note='Body-fat percentage is not established by a camera capture. Record it only from a suitable '
                 'measurement method, with the method noted.',
        )

        return metadata

    @classmethod
    def apply_to_assessment(cls, assessment, result: Dict[str, Any], db_alias: str) -> None:
        """Writes the analysis onto the assessment row."""
        from django.utils import timezone

        summary = result.get('summary') or {}
        circumference = result.get('circumference_estimates') or {}
        values = circumference.get('values') or {}

        def dec(value) -> Optional[Decimal]:
            if value is None:
                return None
            try:
                return Decimal(str(round(float(value), 2)))
            except (TypeError, ValueError):
                return None

        assessment.scan_status = result.get('status') or 'FAILED'
        assessment.scan_provider = result.get('provider') or ''
        assessment.scan_model = result.get('model') or ''
        assessment.scan_completed_at = timezone.now()
        assessment.scan_error = result.get('error') or None
        assessment.scan_capture_quality = result.get('quality') or {}
        assessment.scan_raw_payload = {
            'views_submitted': result.get('views_submitted'),
            'views_usable': result.get('views_usable'),
            'per_view': result.get('per_view'),
            'raw': result.get('raw'),
            'errors': result.get('errors'),
            'warnings': result.get('warnings'),
        }

        assessment.shoulder_tilt_degrees = dec(summary.get('shoulder_tilt_degrees'))
        assessment.hip_tilt_degrees = dec(summary.get('hip_tilt_degrees'))
        assessment.forward_head_angle_degrees = dec(summary.get('forward_head_angle_degrees'))
        assessment.lateral_shift_degrees = dec(summary.get('lateral_shift_degrees'))
        assessment.posture_score = dec(summary.get('posture_score'))

        assessment.estimated_waist_cm = dec(values.get('waist_cm'))
        assessment.estimated_hip_cm = dec(values.get('hip_cm'))
        assessment.estimated_chest_cm = dec(values.get('chest_cm'))
        assessment.estimated_shoulder_width_cm = dec(values.get('shoulder_width_cm'))

        # Waist-to-hip ratio is only meaningful when both numbers come from the same source.
        # Tape measurements win; otherwise fall back to the two estimates together.
        if assessment.waist_circumference_cm and values.get('hip_cm'):
            # A taped waist against an estimated hip would mix sources in one ratio, which the
            # specification warns against. Leave it unset rather than publish a hybrid number.
            assessment.waist_to_hip_ratio = None
        elif values.get('waist_cm') and values.get('hip_cm'):
            try:
                ratio = float(values['waist_cm']) / float(values['hip_cm'])
                assessment.waist_to_hip_ratio = dec(ratio)
            except (TypeError, ValueError, ZeroDivisionError):
                assessment.waist_to_hip_ratio = None

        assessment.cv_analysis_payload = {
            'summary': summary,
            'posture_score_breakdown': result.get('posture_score_breakdown'),
            'circumference_estimates': circumference,
        }

        metadata = dict(assessment.measurement_metadata or {})
        metadata.update(cls.build_measurement_metadata(assessment, result))
        assessment.measurement_metadata = metadata

        assessment.save(using=db_alias)
