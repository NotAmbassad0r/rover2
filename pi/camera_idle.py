"""Camera idle manager — pauses camera proxy when scene is stable.

Principle: when FOLLOW and DETECT are both off and the ultrasonic window
shows a stable scene for long enough, set sleeping=True.  The /stream proxy
stops pulling from rover-camera; rover-camera has no clients and idles
(V4L2 capture stops or drops to minimum).  rover-camera.service is never
stopped — wake latency is <1 s (just reconnect to the already-running process).

Wake triggers:
  1. Ultrasonic reading drifts > distance_variance_cm from the stable baseline.
  2. FOLLOW or DETECT is enabled (notify_tracking_active).
  3. A browser connects to /stream (notify_stream_connect).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Readings at or above this distance are treated as stable open space.
_OPEN_CM = 400.0


class CameraIdleManager:
    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("camera_idle", {})
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._sleep_after_s: float = float(cfg.get("sleep_after_idle_s", 90.0))
        self._variance_cm: float = float(cfg.get("distance_variance_cm", 8.0))
        window: int = max(2, int(cfg.get("window_size", 10)))

        self._window: collections.deque[float] = collections.deque(maxlen=window)
        self._stable_since: float | None = None
        self._sleeping: bool = False
        self._baseline_cm: float | None = None
        self._tracking_active: bool = False

    @property
    def sleeping(self) -> bool:
        return self._sleeping and self._enabled

    # ── Wake interface ────────────────────────────────────────────────────────

    def wake(self, reason: str = "?") -> None:
        if self._sleeping:
            logger.info("CameraIdle: wake (%s)", reason)
        self._sleeping = False
        self._stable_since = None

    def set_tracking_active(self, enabled: bool) -> None:
        """Immediately enable/disable idle suppression when tracking state changes.

        When True the camera never sleeps regardless of scene stability.
        When False normal idle logic resumes (if neither enabled nor detect_only).
        """
        self._tracking_active = enabled
        if enabled:
            self.wake("tracking enabled")

    def notify_tracking_active(self) -> None:
        """Call whenever FOLLOW or DETECT is enabled."""
        self.set_tracking_active(True)

    def notify_stream_connect(self) -> None:
        """Call when any client hits /stream — guarantees immediate wake."""
        self.wake("stream client connected")

    # ── Internal window logic ─────────────────────────────────────────────────

    def _feed(self, cm: float | None, age_s: float | None) -> None:
        """Process one ultrasonic sample (call at most once per poll interval)."""
        if cm is None or cm <= 0 or age_s is None or age_s > 2.0:
            return
        value = float(min(cm, _OPEN_CM))

        if self._sleeping:
            if self._baseline_cm is not None:
                delta = abs(value - self._baseline_cm)
                if delta > self._variance_cm:
                    self.wake(f"ultrasonic delta {delta:.1f} cm")
            return

        self._window.append(value)
        if len(self._window) < self._window.maxlen:
            return  # window not full yet — defer decision

        span = max(self._window) - min(self._window)
        if span <= self._variance_cm:
            if self._stable_since is None:
                self._stable_since = time.monotonic()
            elif time.monotonic() - self._stable_since >= self._sleep_after_s:
                self._go_sleep()
        else:
            self._stable_since = None

    def _go_sleep(self) -> None:
        logger.info(
            "CameraIdle: sleeping (scene stable for %.0fs, variance ≤ %.1f cm)",
            self._sleep_after_s,
            self._variance_cm,
        )
        self._sleeping = True
        self._baseline_cm = (
            sum(self._window) / len(self._window) if self._window else None
        )
        self._window.clear()
        self._stable_since = None

    # ── Background coroutine ──────────────────────────────────────────────────

    async def run(
        self,
        megapi: Any,
        body_tracker: Any | None,
        poll_interval_s: float = 0.5,
    ) -> None:
        """Run as an asyncio task.  Polls at the same rate as the ultrasonic sensor."""
        if not self._enabled:
            return
        logger.info(
            "CameraIdle: started (sleep after %.0fs stable, variance %.1f cm)",
            self._sleep_after_s,
            self._variance_cm,
        )
        while True:
            await asyncio.sleep(poll_interval_s)

            # Never sleep while tracking is active (direct flag or body_tracker state).
            if self._tracking_active or (body_tracker is not None and (
                body_tracker.enabled or body_tracker.detect_only
            )):
                if self._sleeping:
                    self.wake("tracking active")
                self._stable_since = None
                self._window.clear()
                continue

            self._feed(
                getattr(megapi, "last_ultrasonic_cm", None),
                getattr(megapi, "last_ultrasonic_age_s", None),
            )
