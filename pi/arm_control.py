"""Arm lift PWM mapping for ROVER2."""

from __future__ import annotations

from typing import Any


def arm_pwm(direction: str, speed_frac: float, config: dict[str, Any]) -> int:
    """Map UI direction + fraction to signed PWM for the arm motor."""
    arm_cfg = config.get("arm", {})
    max_speed = int(arm_cfg.get("max_speed", 150))
    invert = bool(arm_cfg.get("invert", False))
    direction = direction.lower()
    if direction in ("stop", "off", ""):
        return 0
    magnitude = int(round(max(0.0, min(1.0, speed_frac)) * max_speed))
    if direction == "up":
        pwm = magnitude
    elif direction == "down":
        pwm = -magnitude
    else:
        raise ValueError(f"Unknown arm direction: {direction}")
    if invert:
        pwm = -pwm
    return max(-255, min(255, pwm))
