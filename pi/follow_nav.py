"""Pure helpers for follow-mode steering around obstacles."""

from __future__ import annotations


def steer_around_obstacle(
    wanted: str,
    *,
    forward_blocked: bool,
    person_cx: float | None = None,
    ble_turn: str | None = None,
    default_avoid: str = "left",
) -> str:
    """When forward is blocked, turn toward the person or BLE gradient instead of stopping."""
    if wanted != "forward" or not forward_blocked:
        return wanted
    if person_cx is not None:
        if person_cx < 0.45:
            return "left"
        if person_cx > 0.55:
            return "right"
    if ble_turn in ("left", "right"):
        return ble_turn
    return default_avoid if default_avoid in ("left", "right") else "left"


def ble_should_turn_in_place(
    rssi_history: list[int],
    *,
    min_samples: int = 4,
    drop_dbm: int = 2,
) -> bool:
    """True when RSSI is falling while advancing — rotate to re-home on the beacon."""
    if len(rssi_history) < min_samples:
        return False
    return rssi_history[-1] - rssi_history[-3] < -drop_dbm
