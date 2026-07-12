"""Standalone read-only capture: one live Hailo YOLO inference, saved as a pytest fixture.

Single-shot manual tool — NOT wired into rover2-api, systemd, or any service. Run by hand
on the Pi while a person is (or is not) in frame:

    python3 pi/capture_live_nms.py --name person_scene
    python3 pi/capture_live_nms.py --name empty_scene

Reuses production code rather than reimplementing it:
- body_tracker._HailoInference        — same VDevice/HEF load path body_tracker.py uses for inference.
- body_tracker.TurboJPEG / TJPF_RGB   — same JPEG decoder body_tracker.py uses (same import, same call).
- capture_nms_fixture.save_fixture    — same fixture writer/validator the offline test suite depends on.
- body_tracker_parse.parse_best_person — same parser production uses, for immediate pass/fail feedback.

Frame acquisition (MJPEG frame-boundary scan, then TurboJPEG decode + cv2 resize to 640x640) mirrors
body_tracker.BodyTracker._stream_loop / _process_frame line for line — those are private methods on a
stateful, threaded class with no standalone function to import, so the same library calls and
parameters are replicated here instead of duplicating a parallel frame reader.

Never actuates motors. Never starts, stops, or restarts any service. Runs once, then exits.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from body_tracker import TJPF_RGB, TurboJPEG, _HailoInference  # noqa: E402
from body_tracker_parse import parse_best_person  # noqa: E402
from capture_nms_fixture import save_fixture  # noqa: E402

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_DEFAULT_HEF_PATH = "/opt/rover/models/yolov8m_h10.hef"
_DEFAULT_GROUP_ID = "rover2"
_DEFAULT_CAMERA_URL = "http://127.0.0.1:8081/stream"
_DEFAULT_CONFIDENCE = 0.40

_HAILO_BUSY_MSG = (
    "Hailo busy — stop the API first: sudo systemctl stop rover2-api "
    " (then re-run this, then: sudo systemctl start rover2-api)"
)


def _load_body_tracker_config() -> dict:
    """Minimal YAML read — same pattern pi/main.py uses to load config.yaml."""
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    return data.get("body_tracker", {}) if isinstance(data, dict) else {}


def _camera_health_url(camera_url: str) -> str:
    parts = urlsplit(camera_url)
    return f"{parts.scheme}://{parts.netloc}/health"


def _check_camera_reachable(camera_url: str) -> bool:
    health_url = _camera_health_url(camera_url)
    try:
        with urllib.request.urlopen(health_url, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _fetch_one_jpeg(camera_url: str, timeout_s: float = 10.0) -> bytes:
    """Mirrors body_tracker.BodyTracker._stream_loop's MJPEG frame-boundary scan."""
    req = urllib.request.urlopen(camera_url, timeout=timeout_s)
    buf = b""
    try:
        while True:
            chunk = req.read(4096)
            if not chunk:
                raise RuntimeError("camera stream closed before a full frame arrived")
            buf += chunk
            a = buf.find(b"\xff\xd8")
            b = buf.find(b"\xff\xd9")
            if a == -1 or b == -1 or b < a:
                if len(buf) > 65536:
                    buf = buf[-4096:]
                continue
            return buf[a : b + 2]
    finally:
        req.close()


def _decode_and_resize(jpg: bytes) -> np.ndarray:
    """Mirrors body_tracker.BodyTracker._process_frame's decode + resize (identical params)."""
    tjpeg = TurboJPEG()
    frame = tjpeg.decode(jpg, pixel_format=TJPF_RGB, scaling_factor=(1, 2))
    return cv2.resize(frame, (640, 640)).astype(np.uint8)


def _countdown(seconds: int) -> None:
    print("[capture_live_nms] get into frame, or leave frame, now:")
    for remaining in range(seconds, 0, -1):
        print(f"  capturing in {remaining}...", flush=True)
        time.sleep(1)
    print("  capturing now.", flush=True)


def _expectation_note(name: str, best: tuple | None) -> str:
    lname = name.lower()
    if "empty" in lname:
        return "matches expectation (no person)" if best is None else \
            "does NOT match expectation — expected None for an empty scene"
    if "person" in lname:
        if best is None:
            return "does NOT match expectation — expected a person box, got None"
        score = best[4]
        if 0.4 < score < 0.999:
            return "matches expectation (realistic person-box score)"
        return f"does NOT match expectation — score {score:.3f} outside (0.4, 0.999)"
    return "no expectation known for this --name (use 'person_scene' or 'empty_scene' to auto-check)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="Fixture name, e.g. person_scene or empty_scene")
    parser.add_argument("--delay", type=int, default=8, help="Countdown seconds before capture (default 8)")
    parser.add_argument("--out-dir", default=None, help="Fixture output dir (default tests/fixtures/)")
    args = parser.parse_args()

    cfg = _load_body_tracker_config()
    hef_path = str(cfg.get("hef_path", _DEFAULT_HEF_PATH))
    group_id = str(cfg.get("hailo_group_id", _DEFAULT_GROUP_ID))
    camera_url = str(cfg.get("camera_url", _DEFAULT_CAMERA_URL))
    confidence = float(cfg.get("confidence", _DEFAULT_CONFIDENCE))

    if not _check_camera_reachable(camera_url):
        print(
            f"Camera server unreachable at {_camera_health_url(camera_url)} — "
            "start it first: sudo systemctl start rover2-camera"
        )
        return 1

    print(f"[capture_live_nms] acquiring Hailo device (hef={hef_path}, group={group_id}) ...")
    try:
        hailo_inf = _HailoInference(hef_path, group_id)
    except Exception as exc:
        print(_HAILO_BUSY_MSG)
        print(f"  (underlying error: {exc})")
        return 1

    try:
        _countdown(args.delay)

        try:
            jpg = _fetch_one_jpeg(camera_url)
        except Exception as exc:
            print(f"Frame fetch failed: {exc}")
            return 1

        rgb_640 = _decode_and_resize(jpg)
        nms_out = hailo_inf.infer(rgb_640)
        if nms_out is None:
            print("Hailo inference returned no output — aborting, nothing saved.")
            return 1
    finally:
        hailo_inf.close()

    out_dir = Path(args.out_dir) if args.out_dir else None
    path = save_fixture(nms_out, name=args.name, out_dir=out_dir)

    best = parse_best_person(nms_out, confidence=confidence)
    print(f"[capture_live_nms] saved: {path}")
    print(f"[capture_live_nms] tensor shape={nms_out.shape} size={nms_out.size}")
    if best is None:
        print("[capture_live_nms] parse_best_person: None (no person detected)")
    else:
        y0, x0, y1, x1, score = best
        print(
            f"[capture_live_nms] parse_best_person: score={score:.3f} "
            f"box=(y0={y0:.3f}, x0={x0:.3f}, y1={y1:.3f}, x1={x1:.3f})"
        )
    print(f"[capture_live_nms] {_expectation_note(args.name, best)} — verify before committing the fixture")

    return 0


if __name__ == "__main__":
    sys.exit(main())
