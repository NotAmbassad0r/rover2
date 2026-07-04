"""Fake BLE tracker — controllable RSSI, no radio."""
from __future__ import annotations


class FakeBLETracker:
    """Minimal stand-in for ble_tracker.BLETracker."""

    def __init__(self, rssi: int | None = -65) -> None:
        self._rssi = rssi

    @property
    def rssi(self) -> int | None:
        return self._rssi

    @rssi.setter
    def rssi(self, value: int | None) -> None:
        self._rssi = value

    @property
    def seen(self) -> bool:
        return self._rssi is not None
