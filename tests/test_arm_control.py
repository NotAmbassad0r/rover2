"""Tests for pi/arm_control.py — pure PWM mapping, no hardware."""
from __future__ import annotations

import pytest

from arm_control import arm_pwm


def _cfg(max_speed: int = 150, invert: bool = False) -> dict:
    return {"arm": {"max_speed": max_speed, "invert": invert}}


class TestArmPwm:
    def test_stop_returns_zero(self):
        assert arm_pwm("stop", 1.0, _cfg()) == 0

    def test_off_returns_zero(self):
        assert arm_pwm("off", 1.0, _cfg()) == 0

    def test_empty_direction_returns_zero(self):
        assert arm_pwm("", 1.0, _cfg()) == 0

    def test_up_full_speed(self):
        result = arm_pwm("up", 1.0, _cfg(max_speed=150))
        assert result == 150

    def test_up_half_speed(self):
        result = arm_pwm("up", 0.5, _cfg(max_speed=150))
        assert result == 75

    def test_down_full_speed_is_negative(self):
        result = arm_pwm("down", 1.0, _cfg(max_speed=150))
        assert result == -150

    def test_down_half_speed_is_negative(self):
        result = arm_pwm("down", 0.5, _cfg(max_speed=150))
        assert result == -75

    def test_invert_flips_up_sign(self):
        result = arm_pwm("up", 1.0, _cfg(max_speed=150, invert=True))
        assert result == -150

    def test_invert_flips_down_sign(self):
        result = arm_pwm("down", 1.0, _cfg(max_speed=150, invert=True))
        assert result == 150

    def test_speed_frac_clamped_above_one(self):
        result = arm_pwm("up", 2.0, _cfg(max_speed=150))
        assert result == 150  # frac clamped to 1.0

    def test_speed_frac_clamped_below_zero(self):
        result = arm_pwm("up", -0.5, _cfg(max_speed=150))
        assert result == 0  # frac clamped to 0.0

    def test_max_pwm_clamped_at_255(self):
        result = arm_pwm("up", 1.0, _cfg(max_speed=300))
        assert result == 255

    def test_min_pwm_clamped_at_minus_255(self):
        result = arm_pwm("down", 1.0, _cfg(max_speed=300))
        assert result == -255

    def test_unknown_direction_raises(self):
        with pytest.raises(ValueError, match="Unknown arm direction"):
            arm_pwm("sideways", 1.0, _cfg())

    def test_case_insensitive(self):
        assert arm_pwm("UP", 1.0, _cfg(max_speed=100)) == 100
        assert arm_pwm("DOWN", 1.0, _cfg(max_speed=100)) == -100

    def test_missing_arm_config_uses_defaults(self):
        result = arm_pwm("up", 1.0, {})
        assert result == 150  # default max_speed=150
