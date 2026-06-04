"""Person follow via Pi AI HAT+ 2 (Hailo YOLOv8m) and MJPEG camera stream."""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from body_tracker_parse import parse_best_person
from follow_nav import ble_should_turn_in_place, steer_around_obstacle

if TYPE_CHECKING:
    from ble_tracker import BLETracker
    from safety import SafetyMonitor

logger = logging.getLogger(__name__)

_CONFIRM_FRAMES: int = 3
_LOST_FRAMES: int = 4
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
        self._person_tracked = False
        self._seen_streak = 0
        self._lost_streak = 0
        self._last_drive: tuple[int, int] = (0, 0)
        self._last_drive_time = 0.0
        self._running = False
        self._hailo_ready = False
        self._last_error: str | None = None

        self._safety: SafetyMonitor | None = safety
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

        ble_cfg = (config or {}).get("ble_tracker", {})
        self._ble_rssi_close = int(ble_cfg.get("rssi_close", _BLE_RSSI_CLOSE))
        self._ble_rssi_track = int(ble_cfg.get("rssi_track", _BLE_RSSI_TRACK))
        self._ble_search_speed = int(ble_cfg.get("search_speed", _BLE_SEARCH_SPEED))
        self._ble_fwd_speed = int(ble_cfg.get("fwd_speed", _BLE_FWD_SPEED))

        # External detection callback (e.g. guard mode) — non-blocking, set to None to deregister
        self._detection_cb: Callable[[float, tuple], None] | None = None
        self._detection_cb_lock = threading.Lock()

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
            "person_tracked": self._person_tracked,
            "last_error": self._last_error,
            "camera_url": self._camera_url,
            "follow_mode": self._follow_mode,
            "ble_active": self._ble_active,
            "ble_follow_enabled": self._ble_follow_enabled,
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

    def _update_person_presence(self, seen: bool) -> None:
        was_tracked = self._person_tracked
        if seen:
            self._seen_streak += 1
            self._lost_streak = 0
            if self._seen_streak >= _CONFIRM_FRAMES:
                if not self._person_tracked:
                    logger.info("BodyTracker: person ACQUIRED (streak=%d)", self._seen_streak)
                self._person_tracked = True
        else:
            self._lost_streak += 1
            self._seen_streak = 0
            if self._lost_streak >= _LOST_FRAMES:
                if self._person_tracked:
                    logger.info("BodyTracker: person LOST (streak=%d)", self._lost_streak)
                self._person_tracked = False
        self._person_detected = self._person_tracked

        if was_tracked and not self._person_tracked:
            # Just lost — start the BLE handoff timer
            self._camera_lost_at = time.monotonic()
            self._ble_rssi_history.clear()
        elif not was_tracked and self._person_tracked:
            # Just reacquired — cancel BLE
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
        import cv2

        logger.info("BodyTracker: connecting to %s", self._camera_url)
        req = urllib.request.urlopen(self._camera_url, timeout=10)
        buf = b""
        last_infer = 0.0
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
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9")
                if a == -1 or b == -1 or b < a:
                    if len(buf) > 65536:
                        buf = buf[-4096:]
                    continue
                jpg = buf[a : b + 2]
                buf = buf[b + 2 :]

                now = time.monotonic()
                if now - last_infer < self._frame_interval_s:
                    continue
                last_infer = now

                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue
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

        resized = cv2.resize(frame, (640, 640))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.uint8)
        nms_out = self._hailo.infer(rgb)
        if nms_out is None:
            logger.warning("Hailo infer returned None")
            return
        self._frames_inferred += 1
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

        best = parse_best_person(nms_out, confidence=self._confidence)
        raw_best = parse_best_person(nms_out, confidence=0.0)
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
        else:
            self._last_bbox = None
            self._last_conf = 0.0

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

        if detect_only:
            return

        with self._lock:
            follow_mode = self._follow_mode

        if not self._person_tracked or best is None:
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
            self._follow_drive("left", self._turn_speed, person_cx=cx)
        elif cx > self._right_bound:
            logger.info("Follow: cx=%.2f → RIGHT (speed=%d)", cx, self._turn_speed)
            self._follow_drive("right", self._turn_speed, person_cx=cx)
        elif bbox_w < self._target_bbox_width:
            logger.info("Follow: cx=%.2f bbox=%.2f → FWD  (speed=%d)", cx, bbox_w, self._forward_speed)
            self._follow_drive("forward", self._forward_speed, person_cx=cx)
        else:
            logger.info("Follow: cx=%.2f bbox=%.2f → HOLD", cx, bbox_w)
            self._drive(0, 0)


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
