"""Person follow via Pi AI HAT+ 2 (Hailo YOLOv8m) and MJPEG camera stream."""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from collections import deque
from typing import TYPE_CHECKING, Callable

import numpy as np

from body_tracker_parse import parse_best_person

if TYPE_CHECKING:
    from ble_tracker import BLETracker

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
        self._person_tracked = False
        self._seen_streak = 0
        self._lost_streak = 0
        self._last_drive: tuple[int, int] = (0, 0)
        self._last_drive_time = 0.0
        self._running = False
        self._hailo_ready = False
        self._last_error: str | None = None

        # BLE fallback state
        self._ble: BLETracker | None = ble_tracker
        self._ble_active = False
        self._camera_lost_at: float = 0.0
        self._ble_search_dir: str = "right"
        self._ble_dir_since: float = 0.0
        self._ble_rssi_history: deque[int] = deque(maxlen=_BLE_TREND_WINDOW)

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

    def get_state(self) -> dict:
        state: dict = {
            "available": self.available,
            "running": self._running,
            "enabled": self.enabled,
            "detect_only": self.detect_only,
            "hailo_ready": self._hailo_ready,
            "person_detected": self._person_detected,
            "person_tracked": self._person_tracked,
            "last_error": self._last_error,
            "camera_url": self._camera_url,
            "ble_active": self._ble_active,
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
        self._thread = threading.Thread(target=self._run, name="body-tracker", daemon=True)
        self._thread.start()
        logger.info("BodyTracker started (hef=%s)", self._hef_path)

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
        logger.info("BodyTracker: tracking %s", "ENABLED" if enabled else "DISABLED")
        if not enabled:
            self._reset_tracking_state()
            self._drive(0, 0, force=True)

    def set_detect_only(self, active: bool) -> None:
        with self._lock:
            self._detect_only = active
            if active:
                self._enabled = False
        logger.info("BodyTracker: detect-only %s", "ON" if active else "OFF")
        if not active:
            self._reset_tracking_state()

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

        if rssi is None or rssi < _BLE_RSSI_TRACK:
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
            self._drive_direction(self._ble_search_dir, _BLE_SEARCH_SPEED)
        elif rssi > _BLE_RSSI_CLOSE:
            # Very close — hold
            logger.debug("BLE follow: close (rssi=%d dBm) → HOLD", rssi)
            self._drive(0, 0)
        else:
            # Medium distance — advance
            logger.debug("BLE follow: tracking (rssi=%d dBm) → FWD", rssi)
            self._drive_direction("forward", _BLE_FWD_SPEED)

    def _run(self) -> None:
        self._running = True
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                self._hailo = _HailoInference(self._hef_path, self._hailo_group_id)
                self._hailo_ready = True
                self._last_error = None
                logger.info("BodyTracker: Hailo ready")
                break
            except Exception as exc:
                self._hailo_ready = False
                self._last_error = str(exc)
                if self._stop_event.is_set():
                    return
                logger.warning(
                    "BodyTracker: Hailo init failed (%s) — retry in %.1fs", exc, backoff
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

        while not self._stop_event.is_set():
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

                with self._lock:
                    active = self._enabled or self._detect_only
                if not active:
                    continue

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

        if detect_only:
            return

        if not self._person_tracked or best is None:
            if (
                self._ble is not None
                and self._camera_lost_at > 0.0
                and time.monotonic() - self._camera_lost_at >= _BLE_HANDOFF_DELAY_S
            ):
                if not self._ble_active:
                    self._ble_active = True
                    logger.info(
                        "BLE follow: activated (camera LOST %.1fs ago)",
                        time.monotonic() - self._camera_lost_at,
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
            self._drive_direction("left", self._turn_speed)
        elif cx > self._right_bound:
            logger.info("Follow: cx=%.2f → RIGHT (speed=%d)", cx, self._turn_speed)
            self._drive_direction("right", self._turn_speed)
        elif bbox_w < self._target_bbox_width:
            logger.info("Follow: cx=%.2f bbox=%.2f → FWD  (speed=%d)", cx, bbox_w, self._forward_speed)
            self._drive_direction("forward", self._forward_speed)
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
