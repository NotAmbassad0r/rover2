"""Person follow via Pi AI HAT+ 2 (Hailo YOLOv8m) and MJPEG camera stream."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np
from turbojpeg import TJPF_RGB, TurboJPEG

import emotion_state
from body_tracker_parse import _PERSON_CLASS, nms_output_to_tensor, parse_best_person
from capture_nms_fixture import save_fixture
from follow_nav import ble_should_turn_in_place, steer_around_obstacle

if TYPE_CHECKING:
    from ble_tracker import BLETracker
    from safety import SafetyMonitor

logger = logging.getLogger(__name__)

# ── MJPEG multipart frame extraction ───────────────────────────────────────────
# camera_server.py sends each part as `--frame\r\nContent-Type: ...\r\n
# Content-Length: N\r\n\r\n<N bytes of JPEG>\r\n`. Parse the boundary + declared
# length instead of scanning the rolling buffer for raw SOI/EOI (\xff\xd8 /
# \xff\xd9) markers — the old approach truncated `buf` to its last 4096 bytes
# whenever it grew past 65536 bytes without finding both markers, which could
# discard an already-seen SOI and force the parser to resync on some later
# frame. Diagnosed 2026-07-21: this produced multi-tens-of-seconds stalls in
# body_tracker's own frame consumption while the camera server was verifiably
# still producing frames at full fps (rover2-camera's own /health never
# dipped). Reading the exact declared Content-Length makes desync impossible.
_MULTIPART_BOUNDARY = b"--frame\r\n"
_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


def _extract_multipart_jpeg(buf: bytes) -> tuple[bytes | None, bytes]:
    """Pull one JPEG part out of a multipart/x-mixed-replace buffer.

    Returns (jpeg_bytes, remaining_buf) once a full part is available, or
    (None, buf) if more data is needed first. Critically, `buf` is only ever
    trimmed of bytes that are either definitely-garbage (before the first
    boundary token) or definitely-consumed (part of an already-extracted
    frame) — never of the boundary/header bytes themselves while we're still
    waiting on more of the body to arrive, otherwise the next call would have
    nothing left to find the boundary with.
    """
    boundary_idx = buf.find(_MULTIPART_BOUNDARY)
    if boundary_idx == -1:
        # Haven't seen a boundary yet (e.g. right after connecting) — keep only
        # a short tail in case the boundary token is split across reads.
        if len(buf) > 4096:
            buf = buf[-256:]
        return None, buf
    if boundary_idx > 0:
        buf = buf[boundary_idx:]  # drop garbage before the boundary

    after_boundary = buf[len(_MULTIPART_BOUNDARY):]
    header_end = after_boundary.find(b"\r\n\r\n")
    if header_end == -1:
        return None, buf  # keep the boundary — headers aren't fully in yet

    m = _CONTENT_LENGTH_RE.search(after_boundary[:header_end])
    if not m:
        # Malformed part header — drop it and resync on the next boundary.
        return None, buf[len(_MULTIPART_BOUNDARY) + header_end + 4:]

    length = int(m.group(1))
    body_start = len(_MULTIPART_BOUNDARY) + header_end + 4
    body_end = body_start + length
    if len(buf) < body_end + 2:  # +2 for the trailing \r\n after the JPEG body
        return None, buf  # keep everything — body isn't fully in yet

    return buf[body_start:body_end], buf[body_end + 2:]


# One-shot debug tap (see BodyTracker._maybe_dump_nms): presence of this file triggers
# a single raw-NMS-tensor dump to /tmp on the next inference cycle, then deletes itself.
_NMS_DUMP_TRIGGER_PATH = "/tmp/rover2_dump_nms"

# ── [PIPEDIAG] temporary detection-pipeline diagnostic instrumentation ─────────
# Guarded, off by default, same trigger-file pattern as _NMS_DUMP_TRIGGER_PATH
# above — presence of this file enables verbose [PIPEDIAG] stage-boundary
# logging (rate-limited to ~once/sec per stage); absence is the normal, silent
# default. Purely observational, no behaviour change. Remove after the
# diagnosis is done; see scripts/diag_detection_pipeline.py.
_PIPEDIAG_TRIGGER_PATH = "/tmp/rover2_pipediag"
_pipediag_last_log_ts: dict[str, float] = {}


def _pipediag_on() -> bool:
    return os.path.exists(_PIPEDIAG_TRIGGER_PATH)


def _pipediag_log(stage: str, msg: str) -> None:
    """Rate-limited (~1/s per stage) [PIPEDIAG] log line."""
    now = time.monotonic()
    last = _pipediag_last_log_ts.get(stage, 0.0)
    if now - last >= 1.0:
        _pipediag_last_log_ts[stage] = now
        logger.info("[PIPEDIAG] %s: %s", stage, msg)


# ── [CLASSDIAG] temporary per-class detection diagnostic instrumentation ──────
# Guarded, off by default, same trigger-file pattern as PIPEDIAG/_NMS_DUMP —
# presence of this file enables a per-inference-cycle breakdown of the FULL
# 80-class raw NMS tensor: which classes have any raw score at all, whether the
# hardcoded person class (body_tracker_parse._PERSON_CLASS) has one, and which
# classes would survive the exact same confidence+bounds+size filter
# parse_best_person() applies — computed by calling parse_best_person() once
# per class id, so it is byte-for-byte the same filter logic already running
# in production, never a reimplementation. Purely observational: only reads
# nms_out that's already been computed for this cycle, no extra Hailo calls,
# no change to `best`/`raw_best` used by the real pipeline below. Remove after
# the diagnosis is done; see scripts/diag_person_class.py.
_CLASSDIAG_TRIGGER_PATH = "/tmp/rover2_classdiag"
_CLASSDIAG_RAW_SCORE_FLOOR = 0.05  # ignore near-zero/empty-slot noise when listing raw classes


def _classdiag_on() -> bool:
    return os.path.exists(_CLASSDIAG_TRIGGER_PATH)


def _classdiag_log(msg: str) -> None:
    logger.info("[CLASSDIAG] %s", msg)


# ── [DOWNDIAG] temporary downstream-consumption diagnostic instrumentation ────
# Guarded, off by default, same trigger-file pattern as CLASSDIAG/PIPEDIAG.
# Person detection at the Hailo/parse layer is already proven correct
# (scripts/diag_person_class.py). This tap answers what happens to a CORRECT
# detection next: does the follow-mode decision logic in THIS function
# actually consume `best`/`_person_tracked` each cycle, and what does it
# decide and why. pi/ws_control.py carries its own matching [DOWNDIAG] tap
# (same trigger file, duplicated helper — same pattern camera_server.py
# already uses for its own PIPEDIAG copy, kept decoupled rather than
# cross-importing) for the telemetry-build hop. Purely observational — logs
# only, no change to any decision this function already makes. Remove after
# the diagnosis is done; see scripts/diag_downstream.py.
_DOWNDIAG_TRIGGER_PATH = "/tmp/rover2_downdiag"


def _downdiag_on() -> bool:
    return os.path.exists(_DOWNDIAG_TRIGGER_PATH)


def _downdiag_log(msg: str) -> None:
    logger.info("[DOWNDIAG] %s", msg)


_CONFIRM_FRAMES: int = 3   # default; overridable via config.yaml body_tracker.presence_confirm_frames
_LOST_FRAMES: int = 6      # default; overridable via config.yaml body_tracker.presence_lost_frames
                           # (was 4 — raised alongside the streak-decay change below so a rough
                           # patch during choppy detection doesn't flip "lost" prematurely)
_DRIVE_MIN_INTERVAL_S: float = 0.25
_RECONNECT_DELAY_S: float = 2.0

# BLE fallback constants
_BLE_HANDOFF_DELAY_S: float = 2.0   # seconds after camera LOST before BLE activates
_BLE_RSSI_CLOSE: int = -60           # dBm — person very close, hold
_BLE_RSSI_TRACK: int = -75           # dBm — person visible, advance
_BLE_SEARCH_SPEED: int = 50          # slow rotation while searching
_BLE_FWD_SPEED: int = 60             # slow advance toward beacon
_BLE_DIR_FLIP_S: float = 8.0         # flip search direction after this many seconds
_BLE_TREND_WINDOW: int = 8           # RSSI readings to assess gradient

DriveFn = Callable[[int, int], None]
StopFn = Callable[[], None]


class BodyTracker:
    """Keep a person centred in the camera frame using Hailo person detection."""

    def __init__(
        self,
        drive: DriveFn,
        stop: StopFn,
        directions: dict[str, tuple[int, int]],
        config: dict | None = None,
        ble_tracker: BLETracker | None = None,
        safety: SafetyMonitor | None = None,
        cam_idle: Any | None = None,
    ) -> None:
        cfg = (config or {}).get("body_tracker", {})

        self._drive_fn = drive
        self._stop_fn = stop
        self._directions = directions

        self._camera_url = str(cfg.get("camera_url", "http://127.0.0.1:8081/stream"))
        self._hef_path = str(cfg.get("hef_path", "/opt/rover/models/yolov8m_h10.hef"))
        self._hailo_group_id = str(cfg.get("hailo_group_id", "rover2"))
        self._confidence = float(cfg.get("confidence", 0.40))
        self._centre_zone = float(cfg.get("centre_zone", 0.30))
        self._target_bbox_width = float(cfg.get("target_bbox_width", 0.35))
        self._turn_speed = int(cfg.get("turn_speed", 100))
        self._forward_speed = int(cfg.get("forward_speed", 120))
        self._frame_interval_s = float(cfg.get("frame_interval_s", 0.10))
        self._hailo_warmup_s = float(cfg.get("hailo_warmup_s", 30.0))
        self._detect_only_on_start = bool(cfg.get("detect_only_on_start", False))
        self._min_box_width = float(cfg.get("min_box_width", 0.03))
        self._min_box_height = float(cfg.get("min_box_height", 0.06))
        self._min_box_area = float(cfg.get("min_box_area", 0.003))
        self._confirm_frames = int(cfg.get("presence_confirm_frames", _CONFIRM_FRAMES))
        self._lost_frames = int(cfg.get("presence_lost_frames", _LOST_FRAMES))
        self._start_time = 0.0  # set in start()

        self._left_bound = 0.5 - self._centre_zone / 2
        self._right_bound = 0.5 + self._centre_zone / 2

        self._lock = threading.Lock()
        self._enabled = False
        self._detect_only = False
        self._last_score_log: float = 0.0
        self._frames_inferred: int = 0
        self._last_frame_log: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._hailo: _HailoInference | None = None
        self._person_detected = False
        self._last_bbox: tuple | None = None   # (y0, x0, y1, x1) normalised 0-1
        self._last_conf: float = 0.0
        self._last_cx: float | None = None     # normalised target centre x, for HUD only
        self._last_decision: str = "NONE"      # FWD | LEFT | RIGHT | STOP | NONE — for HUD only
        self._person_tracked = False
        self._seen_streak = 0
        self._lost_streak = 0
        self._last_drive: tuple[int, int] = (0, 0)
        self._last_drive_time = 0.0
        self._running = False
        self._hailo_ready = False
        self._last_error: str | None = None

        self._tjpeg = TurboJPEG()

        self._safety: SafetyMonitor | None = safety
        self._cam_idle: Any | None = cam_idle
        self._avoid_default = str(cfg.get("avoid_default", "left"))

        # Follow mode: "fused" (camera + BLE fallback), "camera" (camera only), "ble" (BLE only)
        self._follow_mode: str = str(cfg.get("follow_mode", "fused"))

        # BLE fallback state
        self._ble: BLETracker | None = ble_tracker
        # ble_follow_enabled derived from follow_mode; config override still respected
        _ble_cfg_default = self._follow_mode in ("fused", "ble")
        self._ble_follow_enabled: bool = bool(cfg.get("ble_follow_enabled", _ble_cfg_default))
        self._ble_active = False
        self._camera_lost_at: float = 0.0
        self._ble_search_dir: str = "right"
        self._ble_dir_since: float = 0.0
        self._ble_rssi_history: deque[int] = deque(maxlen=_BLE_TREND_WINDOW)
        self._ble_rssi_ui: deque[int] = deque(maxlen=30)  # larger history for UI sparkline

        ble_cfg = (config or {}).get("ble_tracker", {})
        self._ble_rssi_close = int(ble_cfg.get("rssi_close", _BLE_RSSI_CLOSE))
        self._ble_rssi_track = int(ble_cfg.get("rssi_track", _BLE_RSSI_TRACK))
        self._ble_search_speed = int(ble_cfg.get("search_speed", _BLE_SEARCH_SPEED))
        self._ble_fwd_speed = int(ble_cfg.get("fwd_speed", _BLE_FWD_SPEED))

        # External detection callback (e.g. guard mode) — non-blocking, set to None to deregister
        self._detection_cb: Callable[[float, tuple], None] | None = None
        self._detection_cb_lock = threading.Lock()

        # Inference telemetry for UI
        self._last_infer_ms: int = 0
        self._last_frame_ts: float = 0.0
        self._actual_fps: float = 0.0

    def set_cam_idle(self, cam_idle: Any | None) -> None:
        """Wire in the CameraIdleManager after construction (cam_idle is created later)."""
        self._cam_idle = cam_idle

    def set_detection_callback(
        self, cb: Callable[[float, tuple], None] | None
    ) -> None:
        """Register a non-blocking callback(confidence, bbox) called on each detection.
        Only one callback at a time. Pass None to deregister. Thread-safe."""
        with self._detection_cb_lock:
            self._detection_cb = cb

    def apply_tuning(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply body_tracker + ble_tracker tuning fields at runtime."""
        applied: dict[str, Any] = {}
        with self._lock:
            if "confidence" in patch:
                self._confidence = float(patch["confidence"])
                applied["confidence"] = self._confidence
            if "centre_zone" in patch:
                self._centre_zone = float(patch["centre_zone"])
                self._left_bound = 0.5 - self._centre_zone / 2
                self._right_bound = 0.5 + self._centre_zone / 2
                applied["centre_zone"] = self._centre_zone
            if "target_bbox_width" in patch:
                self._target_bbox_width = float(patch["target_bbox_width"])
                applied["target_bbox_width"] = self._target_bbox_width
            if "turn_speed" in patch:
                self._turn_speed = int(patch["turn_speed"])
                applied["turn_speed"] = self._turn_speed
            if "forward_speed" in patch:
                self._forward_speed = int(patch["forward_speed"])
                applied["forward_speed"] = self._forward_speed
            if "avoid_default" in patch:
                self._avoid_default = str(patch["avoid_default"])
                applied["avoid_default"] = self._avoid_default
            if "rssi_close" in patch:
                self._ble_rssi_close = int(patch["rssi_close"])
                applied["rssi_close"] = self._ble_rssi_close
            if "rssi_track" in patch:
                self._ble_rssi_track = int(patch["rssi_track"])
                applied["rssi_track"] = self._ble_rssi_track
            if "search_speed" in patch:
                self._ble_search_speed = int(patch["search_speed"])
                applied["search_speed"] = self._ble_search_speed
            if "fwd_speed" in patch:
                self._ble_fwd_speed = int(patch["fwd_speed"])
                applied["fwd_speed"] = self._ble_fwd_speed
        return applied

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def detect_only(self) -> bool:
        with self._lock:
            return self._detect_only

    @property
    def person_detected(self) -> bool:
        return self._person_detected

    @property
    def available(self) -> bool:
        try:
            import hailo_platform  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def frames_inferred(self) -> int:
        return self._frames_inferred

    def is_past_warmup(self) -> bool:
        """True once the Hailo warmup window has elapsed since start()."""
        if self._start_time == 0.0:
            return False
        return time.monotonic() - self._start_time > self._hailo_warmup_s

    def get_state(self) -> dict:
        state: dict = {
            "available": self.available,
            "running": self._running,
            "enabled": self.enabled,
            "detect_only": self.detect_only,
            "hailo_ready": self._hailo_ready,
            "person_detected": self._person_detected,
            "person_bbox": list(self._last_bbox) if self._last_bbox else None,
            "person_conf": round(self._last_conf, 2),
            "person_cx": round(self._last_cx, 3) if self._last_cx is not None else None,
            "follow_decision": self._last_decision,
            "person_tracked": self._person_tracked,
            "last_error": self._last_error,
            "camera_url": self._camera_url,
            "follow_mode": self._follow_mode,
            "ble_active": self._ble_active,
            "ble_follow_enabled": self._ble_follow_enabled,
            "frames_inferred": self._frames_inferred,
            "infer_ms": self._last_infer_ms,
            "actual_fps": self._actual_fps,
            "configured_fps": round(1.0 / self._frame_interval_s, 2) if self._frame_interval_s > 0 else 0,
            "safety_blocked": self._safety.forward_blocked if self._safety else False,
            "safety_distance_cm": self._safety.distance_cm if self._safety else None,
            "safety_threshold_cm": self._safety.safe_distance_cm if self._safety else None,
            "ble_rssi_history": list(self._ble_rssi_ui),
        }
        if self._ble is not None:
            state["ble"] = self._ble.get_state()
        return state

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.available:
            self._last_error = "hailo_platform not installed"
            logger.warning("BodyTracker: %s", self._last_error)
            return
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="body-tracker", daemon=True)
        self._thread.start()
        logger.info("BodyTracker started (hef=%s, warmup=%.0fs)", self._hef_path, self._hailo_warmup_s)

    def stop(self) -> None:
        self.set_enabled(False)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._hailo is not None:
            self._hailo.close()
            self._hailo = None
        self._running = False
        logger.info("BodyTracker stopped")

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            if enabled:
                self._detect_only = False
                if not self._person_tracked and self._camera_lost_at == 0.0:
                    self._camera_lost_at = time.monotonic()
            active = self._enabled or self._detect_only
        if self._cam_idle is not None:
            logger.info("[body_tracker] camera idle %s",
                        "suppressed (tracking active)" if active else "released (tracking inactive)")
            self._cam_idle.set_tracking_active(active)
        logger.info("BodyTracker: tracking %s", "ENABLED" if enabled else "DISABLED")
        if not enabled:
            self._reset_tracking_state()
            try:
                self._drive(0, 0, force=True)
            except RuntimeError as exc:
                logger.warning("BodyTracker: stop_motors skipped — serial not connected: %s", exc)

    def set_follow_mode(self, mode: str) -> None:
        """Set follow mode: 'fused' | 'camera' | 'ble'.

        'fused'  — camera primary, BLE fallback when camera loses person (default)
        'camera' — camera only, no BLE fallback
        'ble'    — BLE-only; skip Hailo inference entirely

        Automatically updates ble_follow_enabled and enabled state.
        """
        if mode not in ("fused", "camera", "ble"):
            logger.warning("BodyTracker: unknown follow_mode %r — ignored", mode)
            return
        with self._lock:
            self._follow_mode = mode
            if mode == "fused":
                self._ble_follow_enabled = True
            elif mode == "camera":
                self._ble_follow_enabled = False
            else:  # ble
                self._ble_follow_enabled = True
                # BLE-only: enabled=True so _run() activates, _run() checks mode
                self._enabled = True
                self._detect_only = False
            active = self._enabled or self._detect_only
        if self._cam_idle is not None:
            logger.info("[body_tracker] camera idle %s",
                        "suppressed (tracking active)" if active else "released (tracking inactive)")
            self._cam_idle.set_tracking_active(active)
        logger.info("BodyTracker: follow_mode=%s ble_follow=%s", mode, self._ble_follow_enabled)

    def set_ble_follow_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._ble_follow_enabled = enabled
        logger.info("BodyTracker: BLE fallback %s", "ON" if enabled else "OFF")

    def set_detect_only(self, active: bool) -> None:
        with self._lock:
            self._detect_only = active
            if active:
                self._enabled = False
            is_active = self._enabled or self._detect_only
        if self._cam_idle is not None:
            logger.info("[body_tracker] camera idle %s",
                        "suppressed (tracking active)" if is_active else "released (tracking inactive)")
            self._cam_idle.set_tracking_active(is_active)
        logger.info("BodyTracker: detect-only %s", "ON" if active else "OFF")
        if not active:
            self._reset_tracking_state()

    def _log_pmic(self, label: str) -> None:
        import subprocess
        try:
            out = subprocess.run(
                ["vcgencmd", "pmic_read_adc"], capture_output=True, text=True, timeout=2
            ).stdout
            interesting = {
                "EXT5V_V", "VDD_CORE_A", "VDD_CORE_V", "1V1_SYS_A", "0V8_SW_A", "3V7_WL_SW_A"
            }
            lines = [l.strip() for l in out.splitlines() if any(k in l for k in interesting)]
            logger.info("PMIC [%s]: %s", label, " | ".join(lines))
        except Exception as exc:
            logger.warning("PMIC read failed (%s): %s", label, exc)

    def _reset_tracking_state(self) -> None:
        self._person_detected = False
        self._person_tracked = False
        self._seen_streak = 0
        self._lost_streak = 0
        self._ble_active = False
        self._camera_lost_at = 0.0
        self._ble_rssi_history.clear()
        self._last_cx = None
        self._last_decision = "NONE"

    def _update_person_presence(self, seen: bool) -> None:
        """Hysteresis gate deciding self._person_tracked (== self._person_detected).

        A miss DECAYS _seen_streak by 1 (floor 0) rather than resetting it to 0 —
        a single dropped frame in an otherwise-good run costs one point of
        progress, not the whole accumulated streak. This tolerates a choppy
        detector (sub-100% per-cycle hit rate on a moving subject) without
        requiring a run of _confirm_frames PERFECTLY consecutive hits, while
        still requiring genuinely sustained presence (not just one lucky frame)
        to confirm. _lost_streak (consecutive misses, no decay) is unchanged —
        that's what still recognizes a genuine departure.
        """
        was_tracked = self._person_tracked
        if seen:
            self._seen_streak += 1
            self._lost_streak = 0
            if self._seen_streak >= self._confirm_frames:
                if not self._person_tracked:
                    logger.info("BodyTracker: person ACQUIRED (streak=%d)", self._seen_streak)
                self._person_tracked = True
        else:
            self._lost_streak += 1
            self._seen_streak = max(0, self._seen_streak - 1)
            if self._lost_streak >= self._lost_frames:
                if self._person_tracked:
                    logger.info("BodyTracker: person LOST (streak=%d)", self._lost_streak)
                self._person_tracked = False
        self._person_detected = self._person_tracked

        if was_tracked and not self._person_tracked:
            # Just lost — start the BLE handoff timer
            self._camera_lost_at = time.monotonic()
            self._ble_rssi_history.clear()
            emotion_state.on_person_lost()
        elif not was_tracked and self._person_tracked:
            # Just reacquired — cancel BLE
            emotion_state.on_person_detected()
            if self._ble_active:
                logger.info("BLE follow: camera re-acquired, returning to camera follow")
            self._ble_active = False
            self._camera_lost_at = 0.0
            self._ble_rssi_history.clear()

    def _forward_blocked(self) -> bool:
        return self._safety is not None and self._safety.forward_blocked

    def _follow_drive(
        self,
        direction: str,
        speed: int,
        *,
        person_cx: float | None = None,
        ble_turn: str | None = None,
    ) -> None:
        """Drive for follow; steer around ultrasonic obstacles instead of stopping."""
        direction = steer_around_obstacle(
            direction,
            forward_blocked=self._forward_blocked(),
            person_cx=person_cx,
            ble_turn=ble_turn,
            default_avoid=self._avoid_default,
        )
        if (
            self._safety is not None
            and direction in ("left", "right")
            and self._forward_blocked()
            and self._safety.distance_cm is not None
        ):
            logger.info(
                "Follow: obstacle at %d cm → %s (keep tracking)",
                self._safety.distance_cm,
                direction.upper(),
            )
        self._drive_direction(direction, speed)

    def _drive_direction(self, direction: str, speed: int) -> None:
        mult_l, mult_r = self._directions.get(direction, (0, 0))
        self._drive(mult_l * speed, mult_r * speed)

    def _drive(self, left: int, right: int, *, force: bool = False) -> None:
        now = time.monotonic()
        cmd = (left, right)
        if (
            not force
            and cmd == self._last_drive
            and now - self._last_drive_time < _DRIVE_MIN_INTERVAL_S
        ):
            return
        if left == 0 and right == 0:
            self._stop_fn()
        else:
            self._drive_fn(left, right)
        self._last_drive = cmd
        self._last_drive_time = now

    def _ble_drive(self) -> None:
        """Drive toward the BLE beacon when the camera has lost the person."""
        if self._ble is None:
            return
        rssi = self._ble.rssi
        now = time.monotonic()

        if rssi is not None:
            self._ble_rssi_history.append(rssi)
            self._ble_rssi_ui.append(rssi)

        if rssi is None or rssi < self._ble_rssi_track:
            # Beacon not visible or too weak — rotate to search
            # Use RSSI trend to decide whether to flip direction
            if len(self._ble_rssi_history) >= _BLE_TREND_WINDOW:
                history = list(self._ble_rssi_history)
                trend = history[-1] - history[0]   # positive = signal improving
                if trend < -3:                      # worsening while turning this way
                    self._ble_search_dir = "left" if self._ble_search_dir == "right" else "right"
                    self._ble_dir_since = now
                    self._ble_rssi_history.clear()
                    logger.info(
                        "BLE follow: signal worsening (trend=%+d dBm), flip → %s",
                        trend, self._ble_search_dir,
                    )
            elif now - self._ble_dir_since > _BLE_DIR_FLIP_S:
                # Timeout fallback — try other direction
                self._ble_search_dir = "left" if self._ble_search_dir == "right" else "right"
                self._ble_dir_since = now
                self._ble_rssi_history.clear()
                logger.info("BLE follow: search timeout, flip → %s", self._ble_search_dir)
            self._follow_drive(self._ble_search_dir, self._ble_search_speed, ble_turn=self._ble_search_dir)
        elif rssi > self._ble_rssi_close:
            # Very close — hold
            logger.debug("BLE follow: close (rssi=%d dBm) → HOLD", rssi)
            self._drive(0, 0)
        elif ble_should_turn_in_place(list(self._ble_rssi_history)):
            logger.debug("BLE follow: RSSI falling → turn %s", self._ble_search_dir)
            self._follow_drive(self._ble_search_dir, self._ble_search_speed, ble_turn=self._ble_search_dir)
        else:
            logger.debug("BLE follow: tracking (rssi=%d dBm) → FWD", rssi)
            self._follow_drive("forward", self._ble_fwd_speed, ble_turn=self._ble_search_dir)

    def _ble_only_loop(self) -> None:
        """BLE-only follow — no camera stream, no Hailo. Runs until mode changes or disabled."""
        logger.info("BodyTracker: BLE-only follow loop started")
        self._ble_active = True
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    active = self._enabled
                    mode = self._follow_mode
                if not active or mode != "ble":
                    break
                if self._ble is not None:
                    self._ble_drive()
                self._stop_event.wait(0.5)
        finally:
            self._ble_active = False
            self._drive(0, 0, force=True)
            logger.info("BodyTracker: BLE-only follow loop stopped")

    def _run(self) -> None:
        self._running = True
        while not self._stop_event.is_set():
            # Idle wait — do not open the stream unless DETECT or FOLLOW is active.
            with self._lock:
                active = self._enabled or self._detect_only
                mode = self._follow_mode
            if not active:
                self._stop_event.wait(0.2)
                continue

            # BLE-only mode: skip Hailo entirely, run BLE drive loop directly.
            if mode == "ble" and self._enabled:
                self._ble_only_loop()
                continue

            # Lazy Hailo init — load model only when first needed, not at service start.
            # Enforce a warmup window so the CPU idles before the model-load power spike.
            if self._hailo is None:
                elapsed = time.monotonic() - self._start_time
                if elapsed < self._hailo_warmup_s:
                    remaining = self._hailo_warmup_s - elapsed
                    logger.info(
                        "BodyTracker: warmup — Hailo load in %.0fs (CPU settling)", remaining
                    )
                    self._stop_event.wait(min(remaining, 2.0))
                    continue

                self._log_pmic("pre-hailo-load")
                backoff = 2.0
                while not self._stop_event.is_set():
                    try:
                        self._hailo = _HailoInference(self._hef_path, self._hailo_group_id)
                        self._hailo_ready = True
                        self._last_error = None
                        self._log_pmic("post-hailo-load")
                        logger.info("BodyTracker: Hailo ready")
                        break
                    except Exception as exc:
                        self._hailo_ready = False
                        self._last_error = str(exc)
                        if self._stop_event.is_set():
                            break
                        logger.warning(
                            "BodyTracker: Hailo init failed (%s) — retry in %.1fs", exc, backoff
                        )
                        self._stop_event.wait(backoff)
                        backoff = min(backoff * 2, 30.0)
                if self._hailo is None:
                    continue

            try:
                self._stream_loop()
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._last_error = str(exc)
                    logger.warning(
                        "BodyTracker: stream error (%s) — reconnect in %.1fs",
                        exc,
                        _RECONNECT_DELAY_S,
                    )
                    self._stop_event.wait(_RECONNECT_DELAY_S)

        self._running = False

    def _stream_loop(self) -> None:
        logger.info("BodyTracker: connecting to %s", self._camera_url)
        req = urllib.request.urlopen(self._camera_url, timeout=10)
        buf = b""
        last_infer = 0.0
        _warmup_frames = 2  # discard dark transition frames after camera reconnect
        try:
            while not self._stop_event.is_set():
                # Stop streaming as soon as DETECT and FOLLOW are both off.
                with self._lock:
                    active = self._enabled or self._detect_only
                if not active:
                    logger.info("BodyTracker: idle — closing stream")
                    break

                chunk = req.read(4096)
                if not chunk:
                    break
                buf += chunk
                jpg, buf = _extract_multipart_jpeg(buf)
                if jpg is None:
                    continue

                if _pipediag_on():
                    _pipediag_log("b_frame_acquisition",
                                  f"jpeg bytes acquired, len={len(jpg)}")

                # Skip dark transition frames immediately after stream reconnect.
                if _warmup_frames > 0:
                    if _warmup_frames == 2:
                        logger.info("[body_tracker] skipping 2 warm-up frames after camera wake")
                    _warmup_frames -= 1
                    continue

                now = time.monotonic()
                if now - last_infer < self._frame_interval_s:
                    # Sleep for most of the remaining interval instead of spin-reading.
                    # Camera generator is unthrottled; without this the loop saturates the CPU
                    # reading and discarding frames at socket speed.
                    remaining = self._frame_interval_s - (now - last_infer)
                    self._stop_event.wait(max(0.0, remaining - 0.10))
                    buf = b""  # clear stale data accumulated during sleep
                    continue
                last_infer = now

                try:
                    # Decode JPEG at 1/2 scale → 320×240 (camera is 640×480).
                    # TurboJPEG DCT-domain scaling: ~4× faster than cv2.imdecode at full res.
                    # Decode directly to RGB — eliminates the subsequent cvtColor step.
                    frame = self._tjpeg.decode(jpg, pixel_format=TJPF_RGB, scaling_factor=(1, 2))
                except Exception as exc:
                    if _pipediag_on():
                        _pipediag_log("c_turbojpeg_decode", f"decode RAISED: {exc!r}")
                    continue
                if _pipediag_on():
                    _pipediag_log(
                        "c_turbojpeg_decode",
                        f"shape={frame.shape} dtype={frame.dtype} "
                        f"all_zero={bool(np.all(frame == 0))}",
                    )
                self._process_frame(frame)
        finally:
            try:
                req.close()
            except Exception:
                pass

    def _process_frame(self, frame: np.ndarray) -> None:
        import cv2

        if self._hailo is None:
            return
        with self._lock:
            active = self._enabled or self._detect_only
            detect_only = self._detect_only
        if not active:
            return

        rgb = cv2.resize(frame, (640, 640)).astype(np.uint8)
        if _pipediag_on():
            _expected_shape = None
            try:
                _expected_shape = self._hailo._infer_model.input().shape  # noqa: SLF001
            except Exception as exc:
                _expected_shape = f"<unavailable: {exc!r}>"
            _pipediag_log(
                "d_preprocess",
                f"handed_to_hailo shape={rgb.shape} dtype={rgb.dtype} "
                f"model_expected_input_shape={_expected_shape}",
            )
        _t_infer = time.monotonic()
        nms_out = self._hailo.infer(rgb)
        _infer_ms = int((time.monotonic() - _t_infer) * 1000)
        if _pipediag_on():
            if nms_out is None:
                _pipediag_log("e_hailo_inference", "infer() returned None")
            else:
                _pipediag_log(
                    "e_hailo_inference",
                    f"shape={nms_out.shape} dtype={nms_out.dtype} "
                    f"all_zero={bool(np.all(nms_out == 0))} infer_ms={_infer_ms}",
                )
        now_fps = time.monotonic()
        if self._last_frame_ts > 0.0:
            dt = now_fps - self._last_frame_ts
            self._actual_fps = round(0.7 * self._actual_fps + 0.3 * (1.0 / dt if dt > 0 else 0.0), 2)
        self._last_frame_ts = now_fps
        self._last_infer_ms = _infer_ms
        if nms_out is None:
            logger.warning("Hailo infer returned None")
            return
        self._frames_inferred += 1
        self._maybe_dump_nms(nms_out)
        logger.debug(
            "Hailo raw: shape=%s max=%.3f mean=%.3f nonzero=%d",
            nms_out.shape, float(nms_out.max()),
            float(nms_out.mean()), int(np.count_nonzero(nms_out)),
        )
        now_fi = time.monotonic()
        if now_fi - self._last_frame_log >= 5.0:
            self._last_frame_log = now_fi
            flat = nms_out.ravel()
            logger.info("Inference running — %d frames, shape=%s max=%.3f",
                        self._frames_inferred, nms_out.shape, float(flat.max()))
            if flat.size == 80 * 501:
                det0 = flat[0:5]
                logger.debug("Class-0 det0: score=%.3f y0=%.3f x0=%.3f y1=%.3f x1=%.3f",
                             det0[0], det0[1], det0[2], det0[3], det0[4])

        _box_kwargs = dict(
            min_box_width=self._min_box_width,
            min_box_height=self._min_box_height,
            min_box_area=self._min_box_area,
        )
        if _pipediag_on():
            _flat = nms_out.ravel()
            _pipediag_log(
                "f_raw_nms_to_parser",
                f"shape={nms_out.shape} size={_flat.size} "
                f"empty={bool(np.all(_flat == 0))} max={float(_flat.max()):.4f}",
            )
        if _classdiag_on():
            _cd_tensor = nms_output_to_tensor(nms_out)
            if _cd_tensor is None:
                _classdiag_log("nms_output_to_tensor returned None — cannot compute per-class breakdown")
            else:
                # Raw max score per class, col 4 = score field, NO filtering at all
                # (not even the 0.01 empty-slot floor parse_best_person applies).
                _cd_raw_scores = _cd_tensor[:, :, 4].max(axis=1)
                _cd_raw_top = sorted(
                    ((i, round(float(s), 3)) for i, s in enumerate(_cd_raw_scores)
                     if s > _CLASSDIAG_RAW_SCORE_FLOOR),
                    key=lambda t: -t[1],
                )
                # Re-run the REAL parse_best_person filter (confidence+bounds+size)
                # once per class id — same function/logic the production path uses
                # two lines below, just parameterized across all 80 classes instead
                # of only the hardcoded person class, to see what would survive if
                # each class were treated as the tracked target.
                _cd_survives = []
                for _cid in range(_cd_tensor.shape[0]):
                    _cd_r = parse_best_person(
                        nms_out, confidence=self._confidence, person_class=_cid, **_box_kwargs
                    )
                    if _cd_r is not None:
                        _cd_survives.append((_cid, round(_cd_r[4], 3)))
                _classdiag_log(
                    f"person_class={_PERSON_CLASS} confidence_threshold={self._confidence:.2f} "
                    f"person_raw_score={float(_cd_raw_scores[_PERSON_CLASS]):.3f} "
                    f"raw_classes(score>{_CLASSDIAG_RAW_SCORE_FLOOR})={_cd_raw_top[:16]} "
                    f"survives_filter={_cd_survives}"
                )
        best = parse_best_person(nms_out, confidence=self._confidence, **_box_kwargs)
        raw_best = parse_best_person(nms_out, confidence=0.0, **_box_kwargs)
        now = time.monotonic()
        if now - self._last_score_log >= 3.0:
            self._last_score_log = now
            if raw_best is not None:
                logger.info("Detect score: %.2f (threshold %.2f) → %s",
                            raw_best[4], self._confidence,
                            "PASS" if best is not None else "below-threshold")
            else:
                logger.info("Detect score: no person boxes")
        self._update_person_presence(best is not None)
        if best is not None:
            self._last_bbox = (best[0], best[1], best[2], best[3])
            self._last_conf = best[4]
            self._last_cx = (best[1] + best[3]) / 2.0
        else:
            self._last_bbox = None
            self._last_conf = 0.0
            self._last_cx = None

        # Fire external detection callback (e.g. guard mode) — must be non-blocking
        if best is not None:
            with self._detection_cb_lock:
                cb = self._detection_cb
            if cb is not None:
                try:
                    _y0b, x0b, _y1b, x1b, score_b = best
                    cb(score_b, (_y0b, x0b, _y1b, x1b))
                except Exception:
                    pass

        if _downdiag_on():
            _downdiag_log(
                f"consumed best={'yes' if best is not None else 'no'} "
                f"bbox={self._last_bbox} conf={self._last_conf:.3f} "
                f"person_tracked={self._person_tracked} detect_only={detect_only} "
                f"follow_mode={self._follow_mode}"
            )

        if detect_only:
            self._last_decision = "NONE"   # HUD bookkeeping only — detect-only never drives
            if _downdiag_on():
                _downdiag_log("decision=NONE reason=detect_only_mode_never_drives")
            return

        with self._lock:
            follow_mode = self._follow_mode

        if not self._person_tracked or best is None:
            self._last_decision = "NONE"   # HUD bookkeeping only — no camera-driven decision
            if _downdiag_on():
                _downdiag_log(
                    f"decision=NONE reason=not_tracked_or_no_best "
                    f"person_tracked={self._person_tracked} best_present={best is not None}"
                )
            if (
                follow_mode != "camera"          # camera mode: never activate BLE
                and self._ble is not None
                and self._ble_follow_enabled
                and self._camera_lost_at > 0.0
                and time.monotonic() - self._camera_lost_at >= _BLE_HANDOFF_DELAY_S
            ):
                if not self._ble_active:
                    self._ble_active = True
                    logger.info(
                        "BLE follow: activated (camera LOST %.1fs ago, mode=%s)",
                        time.monotonic() - self._camera_lost_at,
                        follow_mode,
                    )
                self._ble_drive()
            else:
                self._drive(0, 0)
            return

        _y0, x0, _y1, x1, _score = best
        cx = (x0 + x1) / 2.0
        bbox_w = x1 - x0

        if cx < self._left_bound:
            logger.info("Follow: cx=%.2f → LEFT  (speed=%d)", cx, self._turn_speed)
            self._last_decision = "LEFT"   # HUD bookkeeping only
            if _downdiag_on():
                _downdiag_log(f"decision=LEFT cx={cx:.3f} left_bound={self._left_bound:.3f}")
            self._follow_drive("left", self._turn_speed, person_cx=cx)
        elif cx > self._right_bound:
            logger.info("Follow: cx=%.2f → RIGHT (speed=%d)", cx, self._turn_speed)
            self._last_decision = "RIGHT"   # HUD bookkeeping only
            if _downdiag_on():
                _downdiag_log(f"decision=RIGHT cx={cx:.3f} right_bound={self._right_bound:.3f}")
            self._follow_drive("right", self._turn_speed, person_cx=cx)
        elif bbox_w < self._target_bbox_width:
            logger.info("Follow: cx=%.2f bbox=%.2f → FWD  (speed=%d)", cx, bbox_w, self._forward_speed)
            self._last_decision = "FWD"   # HUD bookkeeping only
            if _downdiag_on():
                _downdiag_log(f"decision=FWD cx={cx:.3f} bbox_w={bbox_w:.3f} target_bbox_width={self._target_bbox_width:.3f}")
            self._follow_drive("forward", self._forward_speed, person_cx=cx)
        else:
            logger.info("Follow: cx=%.2f bbox=%.2f → HOLD", cx, bbox_w)
            self._last_decision = "STOP"   # HUD bookkeeping only — centred + at target distance
            if _downdiag_on():
                _downdiag_log(f"decision=STOP(HOLD) cx={cx:.3f} bbox_w={bbox_w:.3f} — centred at target distance")
            self._drive(0, 0)

    def _maybe_dump_nms(self, nms_out: np.ndarray) -> None:
        """One-shot debug tap: dump the current raw NMS tensor to /tmp when triggered.

        Trigger: presence of _NMS_DUMP_TRIGGER_PATH. Its (stripped) contents are used as
        the fixture name; the file is deleted after firing so it dumps exactly once.
        Purely observational — reuses the existing inference output, opens no new Hailo
        device, and must never affect or crash the tracking loop.
        """
        if not os.path.exists(_NMS_DUMP_TRIGGER_PATH):
            return
        try:
            try:
                with open(_NMS_DUMP_TRIGGER_PATH, "r", encoding="utf-8") as f:
                    name = f.read().strip()
            except OSError:
                name = ""
            name = name or "nms_dump"

            path = save_fixture(nms_out, name=name, out_dir=Path("/tmp"))

            best = parse_best_person(
                nms_out,
                confidence=self._confidence,
                min_box_width=self._min_box_width,
                min_box_height=self._min_box_height,
                min_box_area=self._min_box_area,
            )
            result_str = (
                f"score={best[4]:.3f} box=(y0={best[0]:.3f}, x0={best[1]:.3f}, "
                f"y1={best[2]:.3f}, x1={best[3]:.3f})"
            ) if best is not None else "None"
            logger.info(
                "NMS dump: saved %s shape=%s size=%d parse_best_person=%s",
                path, nms_out.shape, nms_out.size, result_str,
            )
        except Exception as exc:
            logger.warning("NMS dump failed: %s", exc)
        finally:
            try:
                os.remove(_NMS_DUMP_TRIGGER_PATH)
            except OSError:
                pass


class _HailoInference:
    _target = None
    _ref_count = 0

    def __init__(self, hef_path: str, group_id: str) -> None:
        from concurrent.futures import Future as _Future
        from hailo_platform import (  # type: ignore[import]
            HEF,
            FormatType,
            HailoSchedulingAlgorithm,
            VDevice,
        )

        self._Future = _Future
        self._lock = threading.Lock()

        if _HailoInference._target is None:
            params = VDevice.create_params()
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            params.group_id = group_id
            _HailoInference._target = VDevice(params)
        _HailoInference._ref_count += 1
        self._target = _HailoInference._target

        hef = HEF(hef_path)
        self._infer_model = self._target.create_infer_model(hef_path)
        self._infer_model.set_batch_size(1)
        self._infer_model.input().set_format_type(hef.get_input_vstream_infos()[0].format.type)
        for out in self._infer_model.outputs:
            out.set_format_type(FormatType.FLOAT32)
        self._configured = self._infer_model.configure()
        self._out_name = self._infer_model.output_names[0]
        self._out_shape = self._infer_model.output(self._out_name).shape

        if _pipediag_on():
            try:
                import hailo_platform as _hp  # noqa: E402
                _hp_version = getattr(_hp, "__version__", "<unknown>")
            except Exception as exc:
                _hp_version = f"<unavailable: {exc!r}>"
            _pipediag_log(
                "e_hailo_inference_init",
                f"loaded hef={hef_path} group_id={group_id} "
                f"input_shape={self._infer_model.input().shape} "
                f"output_shape={self._out_shape} hailo_platform_version={_hp_version}",
            )

    def infer(self, rgb_640: np.ndarray) -> np.ndarray | None:
        out_buf = np.zeros(self._out_shape, dtype=np.float32)
        output_buffers = {self._out_name: out_buf}
        done = self._Future()

        def _cb(completion_info: object, bindings=None) -> None:
            if getattr(completion_info, "exception", None):
                done.set_exception(completion_info.exception)
            else:
                done.set_result(True)

        with self._lock:
            try:
                bindings = self._configured.create_bindings(output_buffers=output_buffers)
                bindings.input().set_buffer(rgb_640)
                self._configured.wait_for_async_ready(timeout_ms=10000)
                self._configured.run_async([bindings], _cb)
                done.result(timeout=15)
                return out_buf
            except Exception as exc:
                logger.warning("Hailo inference error: %s", exc)
                return None

    def close(self) -> None:
        del self._configured
        _HailoInference._ref_count -= 1
        if _HailoInference._ref_count == 0 and _HailoInference._target is not None:
            _HailoInference._target.release()
            _HailoInference._target = None
