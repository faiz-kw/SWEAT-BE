#!/usr/bin/env python3
"""
Standalone smoke test for the assessment body scan.

Runs the real fal.ai call and the real geometry code against photos on disk, without Django,
a database, Celery or a tenant. Use it to confirm the provider integration works before
wiring anything else up, and to see what the numbers look like on a real body.

    pip install fal-client pillow
    export FAL_KEY=...

    # One view is enough to prove the integration; four gives the full picture.
    python3 scripts/check_body_scan.py --front ~/front.jpg
    python3 scripts/check_body_scan.py \\
        --front ~/front.jpg --back ~/back.jpg \\
        --left ~/left.jpg --right ~/right.jpg --height 170

Pass --dry-run to exercise the geometry and quality gate on a canned provider response
without spending a call or needing a key.
"""

import argparse
import json
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_scan_module():
    """
    Imports services_body_scan with a stub for django.conf, so this script runs in a bare
    interpreter. The module only touches `settings` for configuration defaults.
    """
    if 'django.conf' not in sys.modules:
        try:
            import django.conf  # noqa: F401
        except ImportError:
            django_mod = types.ModuleType('django')
            conf_mod = types.ModuleType('django.conf')

            class _Settings:
                FAL_KEY = os.environ.get('FAL_KEY', '')
                FAL_BODY_SCAN_MODEL = os.environ.get('FAL_BODY_SCAN_MODEL', 'fal-ai/sam-3/3d-body')
                FAL_BODY_SCAN_TIMEOUT_SECONDS = 180
                BODY_SCAN_PROVIDER = os.environ.get('BODY_SCAN_PROVIDER', 'local')
                BODY_SCAN_LOCAL_MODEL_PATH = os.environ.get('BODY_SCAN_LOCAL_MODEL_PATH', '')

            conf_mod.settings = _Settings()
            django_mod.conf = conf_mod
            sys.modules['django'] = django_mod
            sys.modules['django.conf'] = conf_mod

    import importlib.util
    path = REPO_ROOT / 'apps' / 'tenant_core' / 'services_body_scan.py'
    spec = importlib.util.spec_from_file_location('services_body_scan', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_response(view: str):
    """A canned provider response, for --dry-run."""
    names = [
        'nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear',
        'left_shoulder', 'right_shoulder', 'left_elbow', 'right_elbow',
        'left_wrist', 'right_wrist', 'left_hip', 'right_hip',
        'left_knee', 'right_knee', 'left_ankle', 'right_ankle',
    ]
    kp = [[0.0, 0.0] for _ in names]
    if view in ('front', 'back'):
        # A subject standing with a visible 4-degree right-shoulder drop.
        kp[5], kp[6] = [400, 300], [600, 314]
        kp[11], kp[12] = [430, 700], [570, 704]
        kp[15], kp[16] = [440, 1200], [560, 1200]
    else:
        kp[3], kp[5] = [530, 400], [500, 500]
        kp[11], kp[15] = [500, 900], [500, 1400]
    return {
        'metadata': {
            'num_people': 1,
            'keypoint_names': names,
            'people': [{
                'person_id': 0,
                'bbox': [350.0, 180.0, 650.0, 1280.0],
                'focal_length': 1200.0,
                'keypoints_2d': kp,
            }],
        },
        'visualization': {'url': 'https://example.invalid/dry-run.png'},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--front', help='Path to the front standing capture')
    parser.add_argument('--back', help='Path to the back standing capture')
    parser.add_argument('--left', help='Path to the left profile capture')
    parser.add_argument('--right', help='Path to the right profile capture')
    parser.add_argument('--height', type=float, default=None,
                        help='Measured standing height in cm. Without it, girth estimates are skipped by design.')
    parser.add_argument('--provider', choices=('local', 'fal'), default=None,
                        help='Override BODY_SCAN_PROVIDER for this run')
    parser.add_argument('--model-path', default=None,
                        help='Path to the MediaPipe pose_landmarker .task file (local provider)')
    parser.add_argument('--dry-run', action='store_true', help='Use a canned provider response; no key or network needed')
    parser.add_argument('--json', action='store_true', help='Print the full result as JSON')
    args = parser.parse_args()

    bs = load_scan_module()

    views = {}
    for view, path in (('front', args.front), ('back', args.back), ('left', args.left), ('right', args.right)):
        if path:
            resolved = Path(path).expanduser()
            if not resolved.exists():
                print(f"error: {resolved} does not exist", file=sys.stderr)
                return 2
            views[view] = resolved

    if not views and not args.dry_run:
        print("error: pass at least one of --front/--back/--left/--right, or use --dry-run", file=sys.stderr)
        return 2
    if args.dry_run and not views:
        views = {'front': None, 'left': None}

    if not args.dry_run:
        provider = (args.provider or os.environ.get('BODY_SCAN_PROVIDER') or 'local').lower()
        if provider == 'fal':
            key = os.environ.get('FAL_KEY', '').strip()
            if not key:
                print("error: FAL_KEY is not set in the environment.", file=sys.stderr)
                print("       export FAL_KEY=... (or use --provider local / --dry-run)", file=sys.stderr)
                return 2
            client = bs.FalBodyScanClient(api_key=key)
        else:
            model_path = args.model_path or os.environ.get('BODY_SCAN_LOCAL_MODEL_PATH', '')
            client = bs.LocalPoseScanClient(model_path=model_path)
            if not client.is_configured:
                print("error: the MediaPipe pose model was not found.", file=sys.stderr)
                print("       pass --model-path /path/to/pose_landmarker_lite.task", file=sys.stderr)
                print("       download: https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
                      "pose_landmarker_lite/float16/1/pose_landmarker_lite.task", file=sys.stderr)
                return 2
        print(f"provider : {client.provider_name}\nmodel    : {client.model}\n")

    per_view, quality = {}, {}

    for view, path in views.items():
        print(f"[{view}] {path if path else '(dry run)'}")
        if args.dry_run:
            response = fake_response(view)
            image_size = (1000, 1500)
        else:
            image_bytes = path.read_bytes()
            image_size = bs.BodyScanService.image_size(image_bytes)
            content_type = 'image/png' if image_bytes[:8].startswith(b'\x89PNG') else 'image/jpeg'
            try:
                response = client.analyse_image(image_bytes, content_type=content_type)
            except bs.BodyScanError as exc:
                print(f"  FAILED: {exc}\n")
                quality[view] = {'usable': False, 'reason': str(exc)}
                continue

        verdict = bs.assess_capture_quality(response, image_size)
        quality[view] = verdict
        if not verdict.get('usable'):
            print(f"  REJECTED: {verdict.get('reason')}\n")
            continue

        coverage = verdict.get('frame_coverage')
        print(f"  usable (subject fills {coverage:.0%} of frame height)" if coverage else "  usable")

        metadata = response.get('metadata') or {}
        names = metadata.get('keypoint_names') or []
        keypoints = (metadata.get('people') or [{}])[0].get('keypoints_2d') or []
        print(f"  {len(names)} named keypoints, {len(keypoints)} detected")

        if view in ('front', 'back'):
            geometry = bs.analyse_frontal_view(names, keypoints)
        else:
            geometry = bs.analyse_profile_view(names, keypoints, view)
        per_view[view] = geometry

        for key, value in sorted(geometry.items()):
            if key.endswith('_degrees'):
                print(f"    {key:32s} {value:>8.2f} deg")
        print()

    if not per_view:
        print("No view produced geometry. Nothing further to compute.")
        return 1

    front = per_view.get('front') or per_view.get('back') or {}
    profile = per_view.get('left') or per_view.get('right') or {}

    def mean(key, sources):
        values = [s[key] for s in sources if s.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else None

    shoulder = mean('shoulder_tilt_degrees', [per_view.get('front', {}), per_view.get('back', {})])
    hip = mean('hip_tilt_degrees', [per_view.get('front', {}), per_view.get('back', {})])
    lateral = mean('lateral_shift_degrees', [per_view.get('front', {}), per_view.get('back', {})])
    head = mean('forward_head_angle_degrees', [per_view.get('left', {}), per_view.get('right', {})])

    print("=" * 62)
    print("SUMMARY")
    print("=" * 62)
    for label, value in (
        ('shoulder tilt', shoulder), ('hip tilt', hip),
        ('midline shift', lateral), ('forward head', head),
    ):
        print(f"  {label:20s} {'not assessed' if value is None else f'{value:>8.2f} deg'}")

    posture = bs.compute_posture_score(shoulder, hip, head, lateral)
    if posture:
        print(f"\n  posture score        {posture['score']:>8.2f} / 100   ({posture['method']})")
        for component in posture['components']:
            if component['status'] != 'not_assessed':
                print(f"    - {component['metric']:24s} {component['value_degrees']:>6.2f} deg"
                      f"  penalty {component['penalty']:>5.2f}  [{component['status']}]")
    else:
        print("\n  posture score        not assessed (no angle was measurable)")

    girths = bs.estimate_circumferences(front, profile, args.height)
    print(f"\n  girth estimates      [{girths['source']}, confidence={girths['confidence']}]")
    if girths.get('values'):
        for key, value in sorted(girths['values'].items()):
            print(f"    - {key:24s} {value:>8.1f} cm")
        print("    (joint-centre geometry, not a tape replacement — track change, not absolutes)")
    else:
        print(f"    none: {girths.get('unavailable_reason')}")

    if args.json:
        print("\n" + json.dumps(
            {'per_view': per_view, 'quality': quality, 'posture': posture, 'girths': girths},
            indent=2, default=str))

    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
