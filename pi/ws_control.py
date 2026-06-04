"""WebSocket control hub — drive, grip, and live telemetry (Phase 2)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from body_tracker import BodyTracker
from megapi import MegaPiBridge
from safety import SafetyMonitor
from arm_control import arm_pwm

logger = logging.getLogger(__name__)


def build_telemetry(
    megapi: MegaPiBridge,
    safety_monitor: SafetyMonitor | None,
    start_monotonic: float,
    public_ultrasonic_cm: Any,
    body_tracker: BodyTracker | None = None,
    cam_idle: Any = None,
    guard_controller: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "telemetry",
        "status": "ok",
        "serial_connected": megapi.connected,
        "firmware": megapi.firmware_version,
        "motors_ready": megapi.motors_ready,
        "ultrasonic_cm": public_ultrasonic_cm(
            megapi.last_ultrasonic_cm, age_s=megapi.last_ultrasonic_age_s
        ),
        "ultrasonic_age_s": megapi.last_ultrasonic_age_s,
        "ultrasonic_state": megapi.ultrasonic_state,
        "ultrasonic_fault_count": megapi.ultrasonic_fault_count,
        "uptime_s": int(time.monotonic() - start_monotonic),
    }
    if safety_monitor is not None:
        payload["safety_enabled"] = True
        payload["safe_distance_cm"] = safety_monitor.safe_distance_cm
        payload["forward_blocked"] = safety_monitor.forward_blocked
    else:
        payload["safety_enabled"] = False
    payload["camera_sleeping"] = cam_idle.sleeping if cam_idle is not None else False
    if body_tracker is not None:
        state = body_tracker.get_state()
        payload["tracking_available"] = state["available"]
        payload["tracking_enabled"] = state["enabled"]
        payload["tracking_detect_only"] = state["detect_only"]
        payload["tracking_hailo_ready"] = state["hailo_ready"]
        payload["person_detected"] = state["person_detected"]
        payload["person_bbox"] = state.get("person_bbox")
        payload["person_conf"] = state.get("person_conf", 0.0)
        payload["ble_active"] = state.get("ble_active", False)
        payload["ble_follow_enabled"] = state.get("ble_follow_enabled", True)
        payload["follow_mode"] = state.get("follow_mode", "fused")
        ble = state.get("ble")
        if ble:
            payload["ble_available"] = ble.get("available", False)
            payload["ble_seen"] = ble.get("seen", False)
            payload["ble_rssi"] = ble.get("rssi")
        if state.get("last_error"):
            payload["tracking_error"] = state["last_error"]
    else:
        payload["tracking_available"] = False
        payload["tracking_enabled"] = False
        payload["tracking_detect_only"] = False
        payload["ble_active"] = False
        payload["follow_mode"] = "fused"
    if guard_controller is not None:
        payload["guard_state"]      = guard_controller.get_state()
        payload["guard_detections"] = guard_controller.get_stats().get("detections_this_session", 0)
    else:
        payload["guard_state"]      = "DISARMED"
        payload["guard_detections"] = 0
    return payload


class ControlHub:
    """Manages WebSocket clients, drive sessions, and telemetry push."""

    def __init__(
        self,
        megapi: MegaPiBridge,
        directions: dict[str, tuple[int, int]],
        max_speed: int,
        safety_monitor: SafetyMonitor | None,
        start_monotonic: float,
        public_ultrasonic_cm: Any,
        telemetry_interval_s: float = 0.4,
        body_tracker: BodyTracker | None = None,
        config: dict[str, Any] | None = None,
        cam_idle: Any = None,
        guard_controller: Any = None,
    ) -> None:
        self._megapi = megapi
        self._directions = directions
        self._max_speed = max_speed
        self._safety = safety_monitor
        self._start = start_monotonic
        self._public_ultrasonic_cm = public_ultrasonic_cm
        self._telemetry_interval_s = telemetry_interval_s
        self._body_tracker = body_tracker
        self._config = config or {}
        self._cam_idle = cam_idle
        self._guard_controller = guard_controller
        self._clients: set[WebSocket] = set()
        self._driver: WebSocket | None = None
        self._driver_direction: str | None = None
        self._arm_driver: WebSocket | None = None
        self._tracking_owner: WebSocket | None = None
        self._last_client_activity: float = time.monotonic()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._telemetry_extra: dict[str, Any] = {}
        ws_cfg = self._config.get("websocket", {})
        self._heartbeat_timeout_s = float(ws_cfg.get("heartbeat_timeout_s", 4.0))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def touch_activity(self) -> None:
        self._last_client_activity = time.monotonic()

    def set_telemetry_extra(self, extra: dict[str, Any]) -> None:
        self._telemetry_extra = dict(extra)

    def apply_drive_settings(
        self,
        *,
        max_speed: int | None = None,
        default_speed: int | None = None,
    ) -> None:
        if max_speed is not None:
            self._max_speed = int(max_speed)
        if default_speed is not None:
            self._config.setdefault("drive", {})["default_speed"] = int(default_speed)

    def apply_websocket_settings(
        self,
        *,
        telemetry_interval_s: float | None = None,
        heartbeat_timeout_s: float | None = None,
    ) -> None:
        if telemetry_interval_s is not None:
            self._telemetry_interval_s = float(telemetry_interval_s)
        if heartbeat_timeout_s is not None:
            self._heartbeat_timeout_s = float(heartbeat_timeout_s)

    async def watchdog_loop(self) -> None:
        """Stop motion if no client messages (incl. ping) within timeout."""
        try:
            while True:
                await asyncio.sleep(0.5)
                active = (
                    self._driver is not None
                    or self._arm_driver is not None
                    or (
                        self._body_tracker is not None
                        and self._body_tracker.enabled
                    )
                )
                if not active:
                    continue
                if time.monotonic() - self._last_client_activity > self._heartbeat_timeout_s:
                    logger.warning(
                        "Heartbeat timeout (%.1fs) — stopping motors/tracking",
                        self._heartbeat_timeout_s,
                    )
                    self.touch_activity()
                    if self._body_tracker is not None and self._body_tracker.enabled:
                        self._disable_tracking()
                    try:
                        self._megapi.stop_motors()
                        self._megapi.arm(0)
                    except Exception as exc:
                        logger.warning("Heartbeat stop failed: %s", exc)
                    self._driver = None
                    self._driver_direction = None
                    self._arm_driver = None
        except asyncio.CancelledError:
            raise

    def start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self.watchdog_loop())

    def _scale(self, speed_fraction: float) -> int:
        return int(
            max(-self._max_speed, min(self._max_speed, round(speed_fraction * self._max_speed)))
        )

    def _apply_drive(self, direction: str, speed_frac: float) -> tuple[int, int]:
        if direction not in self._directions:
            raise ValueError(f"Unknown direction: {direction}")
        mult_l, mult_r = self._directions[direction]
        speed = self._scale(speed_frac) if direction != "stop" else 0
        left = mult_l * speed
        right = mult_r * speed
        if direction == "stop":
            self._megapi.stop_motors()
        else:
            self._megapi.drive(left, right)
        return left, right

    def _apply_arm(self, direction: str, speed_frac: float) -> int:
        pwm = arm_pwm(direction, speed_frac, self._config)
        self._megapi.arm(pwm)
        return pwm

    async def _send_json(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        await ws.send_json(payload)

    async def _telemetry_loop(self, ws: WebSocket) -> None:
        try:
            while ws in self._clients:
                payload = build_telemetry(
                    self._megapi,
                    self._safety,
                    self._start,
                    self._public_ultrasonic_cm,
                    self._body_tracker,
                    self._cam_idle,
                    self._guard_controller,
                )
                if self._telemetry_extra:
                    payload.update(self._telemetry_extra)
                await self._send_json(ws, payload)
                await asyncio.sleep(self._telemetry_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Telemetry loop ended: %s", exc)

    async def serve(self, ws: WebSocket) -> None:
        """Run session after the route handler has accepted the WebSocket."""
        self._clients.add(ws)
        self.start_watchdog()
        telemetry_task = asyncio.create_task(self._telemetry_loop(ws))
        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    await self._send_json(ws, {"type": "error", "msg": "expected JSON object"})
                    continue
                await self._handle_message(ws, msg)
        except WebSocketDisconnect:
            pass
        finally:
            telemetry_task.cancel()
            try:
                await telemetry_task
            except asyncio.CancelledError:
                pass
            await self._release_driver(ws)

    def _disable_tracking(self) -> None:
        if self._body_tracker is not None:
            self._body_tracker.set_enabled(False)
            self._body_tracker.set_detect_only(False)
        self._tracking_owner = None

    async def _release_driver(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        if self._tracking_owner is ws:
            self._disable_tracking()
            logger.info("WebSocket disconnected — tracking stopped")
        if self._arm_driver is ws:
            self._arm_driver = None
            try:
                self._megapi.arm(0)
                logger.info("WebSocket disconnected — arm stopped")
            except Exception as exc:
                logger.warning("Arm stop on disconnect failed: %s", exc)
        if self._driver is ws:
            self._driver = None
            self._driver_direction = None
            try:
                self._megapi.stop_motors()
                logger.info("WebSocket disconnected — motors stopped")
            except Exception as exc:
                logger.warning("Stop on disconnect failed: %s", exc)

    async def _handle_message(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        self.touch_activity()
        msg_type = str(msg.get("type", "")).lower()

        if msg_type == "ping":
            await self._send_json(ws, {"type": "pong"})
            return

        if msg_type == "tracking":
            enabled = bool(msg.get("enabled", False))
            detect_only = bool(msg.get("detect_only", False))
            if self._body_tracker is None:
                await self._send_json(ws, {"type": "error", "msg": "tracking not configured"})
                return
            if (enabled or detect_only) and not self._body_tracker.available:
                await self._send_json(
                    ws, {"type": "error", "msg": "Hailo not available on this host"}
                )
                return
            if detect_only:
                if self._tracking_owner is not None and self._tracking_owner is not ws:
                    await self._send_json(ws, {"type": "error", "msg": "another client owns tracking"})
                    return
                if self._cam_idle is not None:
                    self._cam_idle.notify_tracking_active()
                self._body_tracker.set_detect_only(True)
                self._tracking_owner = ws
            elif enabled:
                if self._driver is not None and self._driver is not ws:
                    await self._send_json(ws, {"type": "error", "msg": "another client is driving"})
                    return
                self._driver = None
                self._driver_direction = None
                if self._cam_idle is not None:
                    self._cam_idle.notify_tracking_active()
                self._body_tracker.set_enabled(True)
                self._tracking_owner = ws
            else:
                if self._tracking_owner is ws or self._tracking_owner is None:
                    self._disable_tracking()
                try:
                    self._megapi.stop_motors()
                except RuntimeError as exc:
                    await self._send_json(ws, {"type": "error", "msg": str(exc)})
                    return
            await self._send_json(ws, {
                "type": "ack",
                "cmd": "tracking",
                "enabled": self._body_tracker.enabled,
                "detect_only": self._body_tracker.detect_only,
            })
            return

        if msg_type == "drive":
            if self._body_tracker is not None and self._body_tracker.enabled:
                self._disable_tracking()
            direction = str(msg.get("direction", "stop")).lower()
            speed_frac = float(msg.get("speed", 1.0))
            try:
                left, right = self._apply_drive(direction, speed_frac)
            except ValueError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            except RuntimeError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return

            if direction == "stop":
                if self._driver is ws:
                    self._driver = None
                    self._driver_direction = None
            else:
                self._driver = ws
                self._driver_direction = direction

            await self._send_json(
                ws,
                {
                    "type": "ack",
                    "cmd": "drive",
                    "direction": direction,
                    "l": left,
                    "r": right,
                },
            )
            return

        if msg_type == "stop":
            if self._body_tracker is not None and self._body_tracker.enabled:
                self._disable_tracking()
            try:
                self._apply_drive("stop", 0.0)
            except RuntimeError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            if self._driver is ws:
                self._driver = None
                self._driver_direction = None
            await self._send_json(ws, {"type": "ack", "cmd": "stop"})
            return

        if msg_type == "grip":
            action = str(msg.get("action", "")).lower()
            if action not in ("open", "close"):
                await self._send_json(ws, {"type": "error", "msg": "action must be open or close"})
                return
            try:
                self._megapi.grip(action)
            except RuntimeError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            await self._send_json(ws, {"type": "ack", "cmd": "grip", "action": action})
            return

        if msg_type == "arm":
            direction = str(msg.get("direction", "stop")).lower()
            speed_frac = float(msg.get("speed", 1.0))
            try:
                pwm = self._apply_arm(direction, speed_frac)
            except ValueError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            except RuntimeError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            if direction == "stop":
                if self._arm_driver is ws:
                    self._arm_driver = None
            else:
                self._arm_driver = ws
            await self._send_json(
                ws,
                {"type": "ack", "cmd": "arm", "direction": direction, "pwm": pwm},
            )
            return

        if msg_type == "arm_pulse":
            action = str(msg.get("action", "")).lower()
            if action not in ("up", "down"):
                await self._send_json(ws, {"type": "error", "msg": "action must be up or down"})
                return
            try:
                self._megapi.arm(action=action)
            except RuntimeError as exc:
                await self._send_json(ws, {"type": "error", "msg": str(exc)})
                return
            await self._send_json(ws, {"type": "ack", "cmd": "arm_pulse", "action": action})
            return

        await self._send_json(ws, {"type": "error", "msg": f"unknown type: {msg_type}"})
