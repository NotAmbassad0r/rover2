"""BLE beacon tracker — continuous scan for a named device, smoothed RSSI."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)

_RSSI_ALPHA: float = 0.3        # EMA smoothing (0=no update, 1=instant)
_LOST_TIMEOUT_S: float = 3.0    # beacon considered gone after this many seconds
_SCAN_RESTART_S: float = 5.0    # delay before restarting scanner on error


class BLETracker:
    """Scan for a BLE device by name (or MAC) and track its RSSI."""

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or {}).get("ble_tracker", {})
        self._target_name: str = str(cfg.get("device_name", "JBL Flip 6"))
        mac = str(cfg.get("device_mac", "")).strip().upper()
        self._target_mac: str | None = mac or None

        self._lock = threading.Lock()
        self._rssi_smooth: float | None = None
        self._last_seen: float = 0.0
        self._running = False
        self._last_error: str | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ public

    @property
    def rssi(self) -> int | None:
        """Smoothed RSSI in dBm, or None if beacon not seen recently."""
        with self._lock:
            if self._last_seen == 0.0:
                return None
            if time.monotonic() - self._last_seen > _LOST_TIMEOUT_S:
                return None
            return int(self._rssi_smooth) if self._rssi_smooth is not None else None

    @property
    def seen(self) -> bool:
        return self.rssi is not None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def available(self) -> bool:
        try:
            import bleak  # noqa: F401
        except ImportError:
            return False
        return True

    def get_state(self) -> dict:
        return {
            "available": self.available,
            "running": self._running,
            "seen": self.seen,
            "rssi": self.rssi,
            "target_name": self._target_name,
            "last_error": self._last_error,
        }

    def start(self) -> None:
        if not self.available:
            self._last_error = "bleak not installed"
            logger.warning("BLETracker: %s", self._last_error)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="ble-tracker", daemon=True)
        self._thread.start()
        logger.info("BLETracker: started (target=%r)", self._target_name)

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._running = False
        logger.info("BLETracker: stopped")

    # ----------------------------------------------------------------- private

    def _on_advertisement(self, device, adv) -> None:
        name = device.name or ""
        addr = device.address.upper()
        match = (
            (self._target_mac and addr == self._target_mac)
            or (not self._target_mac and self._target_name.lower() in name.lower())
        )
        if not match:
            return
        rssi = adv.rssi
        if rssi is None:
            return
        with self._lock:
            if self._rssi_smooth is None:
                self._rssi_smooth = float(rssi)
            else:
                self._rssi_smooth = (
                    _RSSI_ALPHA * rssi + (1.0 - _RSSI_ALPHA) * self._rssi_smooth
                )
            self._last_seen = time.monotonic()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._scan_forever())
        finally:
            self._running = False
            try:
                self._loop.close()
            except Exception:
                pass

    async def _scan_forever(self) -> None:
        from bleak import BleakScanner  # type: ignore[import]

        self._running = True
        while not self._stop_event.is_set():
            try:
                scanner = BleakScanner(detection_callback=self._on_advertisement)
                await scanner.start()
                logger.info("BLETracker: scanner active, looking for %r", self._target_name)
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.1)
                await scanner.stop()
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning(
                    "BLETracker: error (%s) — restart in %.0fs", exc, _SCAN_RESTART_S
                )
                await asyncio.sleep(_SCAN_RESTART_S)
