"""Unit tests for arm lift PWM mapping."""

from __future__ import annotations

import unittest

from arm_control import arm_pwm


class ArmPwmTests(unittest.TestCase):
    def test_up_and_down(self) -> None:
        cfg = {"arm": {"max_speed": 150, "invert": False}}
        self.assertEqual(arm_pwm("up", 1.0, cfg), 150)
        self.assertEqual(arm_pwm("down", 0.5, cfg), -75)
        self.assertEqual(arm_pwm("stop", 1.0, cfg), 0)

    def test_invert(self) -> None:
        cfg = {"arm": {"max_speed": 100, "invert": True}}
        self.assertEqual(arm_pwm("up", 1.0, cfg), -100)
        self.assertEqual(arm_pwm("down", 1.0, cfg), 100)


if __name__ == "__main__":
    unittest.main()
