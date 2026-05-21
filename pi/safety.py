"""Ultrasonic obstacle safety for ROVER2.

Polls the MegaPi ultrasonic sensor and blocks forward drive commands when an
obstacle is closer than the configured threshold. Turning and reversing are
always allowed.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from megapi import MegaPiBridge

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any]], None]


class SafetyMonitor:
    """Wraps :class:`MegaPiBridge.send` to gate forward motion."""

    def __init__(
        self,
        megapi: MegaPiBridge,
        forward_signs: tuple[int, int],
        safe_distance_cm: int = 40,
        poll_interval_s: float = 0.5,
        initial_poll_delay_s: float = 4.0,
    ) -> None:
        self._megapi = megapi
        self._forward_signs = forward_signs
        self._safe_distance_cm = safe_distance_cm
        self._poll_interval_s = poll_interval_s
        self._initial_poll_delay_s = initial_poll_delay_s

        self._distance_cm: int | None = None
        self._forward_blocked: bool = False
        self._lock = threading.Lock()
        self._poll_timer: threading.Timer | None = None
        self._stopped = True
        self._orig_send: SendFn | None = None
        self._last_block_log: float = 0.0

    @property
    def forward_blocked(self) -> bool:
        with self._lock:
            return self._forward_blocked

    @property
    def safe_distance_cm(self) -> int:
        return self._safe_distance_cm

    def set_safe_distance_cm(self, cm: int) -> None:
        self._safe_distance_cm = int(cm)

    @property
    def distance_cm(self) -> int | None:
        with self._lock:
            return self._distance_cm

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._orig_send = self._megapi.send
        self._megapi.send = self._guarded_send  # type: ignore[method-assign]

        prev_on_message = self._megapi.on_message

        def _on_message(msg: dict[str, Any]) -> None:
            self._ingest_ultrasonic(msg)
            if prev_on_message:
                prev_on_message(msg)

        self._megapi.on_message = _on_message
        logger.info(
            "SafetyMonitor started — threshold %d cm, forward signs %s",
            self._safe_distance_cm,
            self._forward_signs,
        )

    def stop(self) -> None:
        self._stopped = True
        if self._orig_send is not None:
            self._megapi.send = self._orig_send  # type: ignore[method-assign]
            self._orig_send = None

    def _guarded_send(self, command: dict[str, Any]) -> None:
        assert self._orig_send is not None
        if self._should_block(command):
            now = time.monotonic()
            if now - self._last_block_log >= 2.0:
                logger.warning(
                    "Forward drive blocked — obstacle at %s cm (limit %d cm)",
                    self._distance_cm,
                    self._safe_distance_cm,
                )
                self._last_block_log = now
            self._orig_send({"cmd": "drive", "l": 0, "r": 0})
            with self._lock:
                self._forward_blocked = True
            return
        with self._lock:
            if command.get("cmd") == "drive":
                left = int(command.get("l", 0))
                right = int(command.get("r", 0))
                if not self._is_forward(left, right):
                    self._forward_blocked = False
        self._orig_send(command)

    def _should_block(self, command: dict[str, Any]) -> bool:
        if command.get("cmd") != "drive":
            return False
        left = int(command.get("l", 0))
        right = int(command.get("r", 0))
        if not self._is_forward(left, right):
            return False
        with self._lock:
            dist = self._distance_cm
        return dist is not None and 0 < dist < self._safe_distance_cm

    def _is_forward(self, left: int, right: int) -> bool:
        """True when motor signs match the configured forward direction."""
        if left == 0 and right == 0:
            return False
        fl, fr = self._forward_signs
        if fl == 0 or fr == 0:
            return False
        return (left > 0) == (fl > 0) and (right > 0) == (fr > 0)

    def _ingest_ultrasonic(self, msg: dict[str, Any]) -> None:
        if msg.get("evt") != "sensor" and msg.get("type") != "sensor":
            return
        if "ultrasonic_cm" not in msg:
            return
        raw = msg.get("ultrasonic_cm")
        with self._lock:
            if raw is None:
                self._distance_cm = None
            else:
                try:
                    val = int(raw)
                    self._distance_cm = val if val > 0 else None
                except (TypeError, ValueError):
                    self._distance_cm = None
            if self._distance_cm is None or self._distance_cm >= self._safe_distance_cm:
                self._forward_blocked = False

