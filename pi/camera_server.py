#!/usr/bin/env python3
"""
ROVER2-native MJPEG camera server — replaces v1 rover-camera.service.

Port 8081 (same as v1):
  GET /stream    — multipart/x-mixed-replace MJPEG stream
  GET /snapshot  — single JPEG frame
  GET /health    — JSON: {"status", "fps", "clients"}

Improvements over v1:
  - TurboJPEG encoding (~2x faster than cv2.imencode)
  - Idle mode: 1fps when no clients connected (saves CPU)
  - Frame notification via Condition — stream handlers wake exactly on new frames
  - SIGTERM / SIGINT clean shutdown
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time

import cv2
from turbojpeg import TurboJPEG

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [rover2-camera] %(message)s",
)
logger = logging.getLogger("rover2-camera")

# ── Config from environment (same vars as v1 rover-camera.service) ───────────

DEVICE   = os.environ.get("ROVER_CAMERA_DEVICE", "/dev/video0")
WIDTH    = int(os.environ.get("ROVER_CAMERA_WIDTH", "640"))
HEIGHT   = int(os.environ.get("ROVER_CAMERA_HEIGHT", "480"))
FPS      = int(os.environ.get("ROVER_CAMERA_FPS", "15"))
QUALITY  = int(os.environ.get("ROVER_CAMERA_QUALITY", "85"))
PORT     = int(os.environ.get("ROVER_CAMERA_PORT", "8081"))

_IDLE_FPS     = 1.0
_RECONNECT_S  = 3.0
_FRAME_S      = 1.0 / max(FPS, 1)
_IDLE_FRAME_S = 1.0 / _IDLE_FPS

# ── Shared frame state ────────────────────────────────────────────────────────

_turbo = TurboJPEG()

_frame_lock    = threading.Lock()
_frame_cond    = threading.Condition(_frame_lock)
_latest_frame: bytes | None = None
_frame_counter = 0        # increments on every new frame; handlers track this

_client_lock   = threading.Lock()
_client_count  = 0

_stop_event    = threading.Event()
_actual_fps    = 0.0


def _publish_frame(data: bytes) -> None:
    global _latest_frame, _frame_counter
    with _frame_cond:
        _latest_frame = data
        _frame_counter += 1
        _frame_cond.notify_all()


def _get_latest_frame() -> bytes | None:
    with _frame_cond:
        return _latest_frame


def _wait_for_frame(last: int, timeout: float = 2.0) -> tuple[bytes | None, int]:
    """Block until a frame newer than `last` arrives or timeout expires."""
    with _frame_cond:
        _frame_cond.wait_for(lambda: _frame_counter != last, timeout=timeout)
        return _latest_frame, _frame_counter


def _add_client() -> None:
    global _client_count
    with _client_lock:
        _client_count += 1
        n = _client_count
    logger.info("client connected (total: %d)", n)


def _remove_client() -> None:
    global _client_count
    with _client_lock:
        _client_count = max(0, _client_count - 1)
        n = _client_count
    logger.info("client disconnected (total: %d)", n)


def _get_client_count() -> int:
    with _client_lock:
        return _client_count


# ── Camera capture thread ─────────────────────────────────────────────────────


def _capture_thread() -> None:
    logger.info("started — %s %dx%d @ %dfps quality=%d port=%d",
                DEVICE, WIDTH, HEIGHT, FPS, QUALITY, PORT)
    while not _stop_event.is_set():
        cap = _open_camera()
        if cap is None:
            _stop_event.wait(timeout=_RECONNECT_S)
            continue
        try:
            _capture_loop(cap)
        except Exception as exc:
            logger.warning("capture error: %s — reconnecting", exc)
        finally:
            cap.release()
        if not _stop_event.is_set():
            logger.info("reconnecting in %.1fs", _RECONNECT_S)
            _stop_event.wait(timeout=_RECONNECT_S)


def _open_camera() -> cv2.VideoCapture | None:
    cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        logger.error("cannot open %s — will retry", DEVICE)
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    logger.info("camera opened: %dx%d @ %.1ffps", w, h, actual_fps)
    return cap


def _capture_loop(cap: cv2.VideoCapture) -> None:
    global _actual_fps
    failures  = 0
    was_idle  = False
    last_ts   = time.monotonic()

    while not _stop_event.is_set():
        idle = _get_client_count() == 0

        if idle != was_idle:
            logger.info("mode → %s", "idle 1fps (0 clients)" if idle else f"active {FPS}fps")
            was_idle = idle

        ret, frame = cap.read()
        if not ret or frame is None:
            failures += 1
            if failures >= 5:
                raise RuntimeError(f"{failures} consecutive read failures")
            time.sleep(0.1)
            continue
        failures = 0

        try:
            jpeg = _turbo.encode(frame, quality=QUALITY)
        except Exception as exc:
            logger.warning("TurboJPEG encode error: %s", exc)
            continue

        _publish_frame(jpeg)

        now     = time.monotonic()
        elapsed = max(now - last_ts, 1e-6)
        _actual_fps = 0.9 * _actual_fps + 0.1 * (1.0 / elapsed)
        last_ts = now

        _stop_event.wait(timeout=_IDLE_FRAME_S if idle else _FRAME_S)


# ── HTTP handler ──────────────────────────────────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/stream":
            self._stream()
        elif path == "/snapshot":
            self._snapshot()
        elif path == "/health":
            self._health()
        else:
            self.send_error(404)

    def _stream(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except OSError:
            return

        _add_client()
        try:
            seen = 0
            while not _stop_event.is_set():
                frame, seen = _wait_for_frame(seen)
                if frame is None:
                    continue
                try:
                    chunk = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                        b"\r\n" + frame + b"\r\n"
                    )
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            _remove_client()

    def _snapshot(self) -> None:
        frame = _get_latest_frame()
        if frame is None:
            self.send_error(503, "No frame available")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
        except OSError:
            pass

    def _health(self) -> None:
        body = json.dumps({
            "status": "ok" if _get_latest_frame() is not None else "no_frame",
            "fps": round(_actual_fps, 1),
            "clients": _get_client_count(),
        }).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # suppress per-request HTTP logging


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads    = True
    allow_reuse_address = True


# ── Entry point ───────────────────────────────────────────────────────────────


def _shutdown(sig: int, _frame: object) -> None:
    logger.info("signal %d — shutting down", sig)
    _stop_event.set()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    t = threading.Thread(target=_capture_thread, daemon=True, name="capture")
    t.start()

    server = _Server(("0.0.0.0", PORT), _Handler)
    logger.info("listening on port %d", PORT)
    try:
        server.serve_forever()
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
