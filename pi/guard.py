"""Door guard mode — BLE-triggered arm/disarm, Hailo DETECT for intruder alerting.

State machine:
  DISARMED  — owner present (beacon RSSI >= rssi_present_threshold)
  ARMING    — RSSI dropped, debounce counting (rssi_leaving_debounce_s)
  ARMED     — owner away; detection callback active; zero motor commands
  ALERT     — intruder detected; HA webhook fired; hold for alert_hold_s
  DISARMING — beacon reappeared; brief hold before DISARMED
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ble_tracker import BLETracker
    from body_tracker import BodyTracker

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S: float = 5.0
_DISARMING_HOLD_S: float = 2.0


class GuardController:
    """Stationary door guard — pure vision sensor, motors never commanded."""

    DISARMED  = "DISARMED"
    ARMING    = "ARMING"
    ARMED     = "ARMED"
    ALERT     = "ALERT"
    DISARMING = "DISARMING"

    def __init__(
        self,
        config: dict,
        ble_tracker: BLETracker | None,
        body_tracker: BodyTracker | None,
    ) -> None:
        cfg = config.get("guard", {})
        self._enabled: bool                 = bool(cfg.get("enabled", False))
        self._rssi_present: int             = int(cfg.get("rssi_present_threshold", -70))
        self._rssi_return: int              = int(cfg.get("rssi_return_threshold", -65))
        self._leaving_debounce_s: float     = float(cfg.get("rssi_leaving_debounce_s", 10.0))
        self._intruder_confidence: float    = float(cfg.get("intruder_confidence", 0.55))
        self._alert_hold_s: float           = float(cfg.get("alert_hold_s", 30.0))
        self._speak_on_change: bool         = bool(cfg.get("speak_on_state_change", True))

        ha_base = str(cfg.get("ha_base_url", "")).rstrip("/")
        self._ha_base:        str = ha_base
        self._ha_token:       str = str(cfg.get("ha_token", ""))
        self._webhook_intruder:  str = str(cfg.get("webhook_intruder_id",  "rover_intruder"))
        self._webhook_armed:     str = str(cfg.get("webhook_armed_id",     "rover_armed"))
        self._webhook_disarmed:  str = str(cfg.get("webhook_disarmed_id",  "rover_disarmed"))

        self._ble:  BLETracker  | None = ble_tracker
        self._body: BodyTracker | None = body_tracker

        # Speak fn set by create_app() once audio_router is available
        self._speak_fn: Callable[[str], None] | None = None

        # Runtime state
        self._state: str = self.DISARMED
        self._armed_since_t: float | None  = None   # wall-clock (time.time())
        self._alert_since_m: float | None  = None   # monotonic for interval calc
        self._below_since_m: float | None  = None   # when RSSI first dropped
        self._detections: int              = 0
        self._last_detection_t: float | None = None  # wall-clock
        self._last_webhook_ok: bool | None   = None
        self._last_webhook_at_t: float | None = None  # wall-clock
        self._last_intruder_speak_m: float   = 0.0
        _INTRUDER_SPEAK_DEBOUNCE_S          = 60.0
        self._intruder_speak_debounce        = _INTRUDER_SPEAK_DEBOUNCE_S

        self._task: asyncio.Task | None = None
        self._running: bool = False

    # ───────────────────────────────────────────── public API

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_speak_fn(self, fn: Callable[[str], None] | None) -> None:
        """Called by create_app() once audio_router is ready."""
        self._speak_fn = fn

    def get_state(self) -> str:
        return self._state

    def get_stats(self) -> dict:
        return {
            "guard_enabled":           self._enabled,
            "state":                   self._state,
            "beacon_rssi":             self._ble.rssi if self._ble else None,
            "armed_since":             _iso_wall(self._armed_since_t),
            "detections_this_session": self._detections,
            "last_detection":          _iso_wall(self._last_detection_t),
            "last_webhook_ok":         self._last_webhook_ok,
            "last_webhook_at":         _iso_wall(self._last_webhook_at_t),
        }

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._transition(self.DISARMED)

    async def manual_override(self, action: str) -> str:
        """arm / disarm / enable / disable — returns new state string."""
        if action == "enable":
            self.set_enabled(True)
        elif action == "disable":
            self.set_enabled(False)
        elif action == "arm":
            self._transition(self.ARMED)
        elif action == "disarm":
            self._transition(self.DISARMED)
        return self._state

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="guard-monitor")
        logger.info("GuardController: started (enabled=%s)", self._enabled)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._deregister_callback()
        logger.info("GuardController: stopped")

    # ───────────────────────────────────────────── state machine

    def _transition(self, new_state: str) -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        logger.info("Guard: %s → %s", old, new_state)

        if new_state == self.ARMING:
            pass  # transitional — no side effects yet

        elif new_state == self.ARMED:
            self._armed_since_t = time.time()
            self._alert_since_m = None
            self._register_callback()
            self._speak("GUARD_ARMED")
            asyncio.ensure_future(self._fire_webhook(
                self._webhook_armed, {"event": "armed"}
            ))

        elif new_state == self.DISARMED:
            self._armed_since_t = None
            self._alert_since_m = None
            self._below_since_m = None
            self._deregister_callback()
            if old in (self.ARMED, self.ALERT, self.ARMING, self.DISARMING):
                self._speak("GUARD_DISARMED")
                asyncio.ensure_future(self._fire_webhook(
                    self._webhook_disarmed, {"event": "disarmed"}
                ))

        elif new_state == self.ALERT:
            self._alert_since_m  = time.monotonic()
            self._detections    += 1
            self._last_detection_t = time.time()

        elif new_state == self.DISARMING:
            pass  # brief hold, then DISARMED via _check_state

    async def _monitor_loop(self) -> None:
        """asyncio task — polls BLE every 5 s; zero CPU when sleeping."""
        while self._running:
            try:
                if self._enabled:
                    await self._check_state()
            except Exception as exc:
                logger.warning("Guard: monitor error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _check_state(self) -> None:
        rssi = self._ble.rssi if self._ble else None
        now  = time.monotonic()

        if self._state == self.DISARMED:
            if rssi is None or rssi < self._rssi_present:
                self._below_since_m = self._below_since_m or now
                if now - self._below_since_m >= self._leaving_debounce_s:
                    self._transition(self.ARMING)
                    self._transition(self.ARMED)
                    self._below_since_m = None
            else:
                self._below_since_m = None

        elif self._state == self.ARMING:
            # Shouldn't linger here — transition happens inline in DISARMED check
            # Belt-and-suspenders: if somehow stuck here, push to ARMED immediately
            self._transition(self.ARMED)

        elif self._state == self.ARMED:
            if rssi is not None and rssi >= self._rssi_return:
                self._transition(self.DISARMING)
                await asyncio.sleep(_DISARMING_HOLD_S)
                if self._state == self.DISARMING:
                    self._transition(self.DISARMED)

        elif self._state == self.ALERT:
            # Owner returned mid-alert?
            if rssi is not None and rssi >= self._rssi_return:
                self._transition(self.DISARMING)
                await asyncio.sleep(_DISARMING_HOLD_S)
                if self._state == self.DISARMING:
                    self._transition(self.DISARMED)
                return
            # Auto-clear after hold period
            if (
                self._alert_since_m is not None
                and now - self._alert_since_m >= self._alert_hold_s
            ):
                self._speak("GUARD_ALERT_CLEARED")
                self._transition(self.ARMED)

        elif self._state == self.DISARMING:
            # Handled inline after sleep above; nothing needed here
            pass

    # ───────────────────────────────────────────── detection callback

    def _register_callback(self) -> None:
        if self._body is not None:
            self._body.set_detection_callback(self._on_person_detected)
            logger.debug("Guard: detection callback registered")

    def _deregister_callback(self) -> None:
        if self._body is not None:
            self._body.set_detection_callback(None)
            logger.debug("Guard: detection callback deregistered")

    def _on_person_detected(self, confidence: float, bbox: tuple) -> None:
        """Called from Hailo inference thread — must be non-blocking."""
        if self._state != self.ARMED:
            return
        if confidence < self._intruder_confidence:
            return
        # Confirm beacon still absent
        rssi = self._ble.rssi if self._ble else None
        if rssi is not None and rssi >= self._rssi_return:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._handle_intruder, confidence, bbox)
        except RuntimeError:
            pass

    def _handle_intruder(self, confidence: float, bbox: tuple) -> None:
        """Runs on asyncio thread via call_soon_threadsafe."""
        if self._state != self.ARMED:
            return
        self._transition(self.ALERT)
        now = time.monotonic()
        if now - self._last_intruder_speak_m >= self._intruder_speak_debounce:
            self._last_intruder_speak_m = now
            self._speak("GUARD_INTRUDER")
        rssi = self._ble.rssi if self._ble else None
        asyncio.ensure_future(self._fire_webhook(
            self._webhook_intruder,
            {
                "event":      "intruder_detected",
                "confidence": round(confidence, 3),
                "timestamp":  _iso_now(),
                "beacon_rssi": rssi,
            },
        ))

    # ───────────────────────────────────────────── HA webhook

    async def _fire_webhook(self, webhook_id: str, body: dict) -> None:
        if not self._ha_base:
            return
        url = f"{self._ha_base}/api/webhook/{webhook_id}"
        body.setdefault("timestamp", _iso_now())
        try:
            import httpx
            headers: dict[str, str] = {}
            if self._ha_token:
                headers["Authorization"] = f"Bearer {self._ha_token}"
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                resp = await client.post(url, json=body, headers=headers)
            self._last_webhook_ok  = resp.status_code < 400
            self._last_webhook_at_t = time.time()
            logger.info("Guard: webhook %s → HTTP %d", webhook_id, resp.status_code)
        except Exception as exc:
            self._last_webhook_ok  = False
            self._last_webhook_at_t = time.time()
            logger.warning("Guard: webhook %s failed: %s", webhook_id, exc)

    # ───────────────────────────────────────────── speak helper

    def _speak(self, event_key: str) -> None:
        if not self._speak_on_change or self._speak_fn is None:
            return
        try:
            self._speak_fn(event_key)
        except Exception as exc:
            logger.debug("Guard: speak_fn error: %s", exc)


# ─────────────────────────────────────────────────────────── helpers

def _iso_wall(wall_t: float | None) -> str | None:
    if wall_t is None:
        return None
    return datetime.fromtimestamp(wall_t, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
