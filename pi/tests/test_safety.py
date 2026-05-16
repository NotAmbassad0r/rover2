"""Unit tests for ROVER2 safety forward detection."""

from __future__ import annotations

import unittest

from safety import SafetyMonitor


class ForwardDetectionTests(unittest.TestCase):
    def _monitor(self, forward: tuple[int, int]) -> SafetyMonitor:
        return SafetyMonitor(
            megapi=object(),  # type: ignore[arg-type]
            forward_signs=forward,
        )

    def test_standard_both_positive_forward(self) -> None:
        m = self._monitor((1, 1))
        self.assertTrue(m._is_forward(100, 100))
        self.assertFalse(m._is_forward(-100, -100))
        self.assertFalse(m._is_forward(-100, 100))

    def test_rover2_wiring_forward(self) -> None:
        m = self._monitor((1, -1))
        self.assertTrue(m._is_forward(120, -120))
        self.assertFalse(m._is_forward(120, 120))
        self.assertFalse(m._is_forward(-120, 120))

    def test_block_only_close_obstacle(self) -> None:
        m = self._monitor((1, -1))
        m._distance_cm = 30
        self.assertTrue(m._should_block({"cmd": "drive", "l": 100, "r": -100}))
        m._distance_cm = 50
        self.assertFalse(m._should_block({"cmd": "drive", "l": 100, "r": -100}))
        m._distance_cm = -1
        self.assertFalse(m._should_block({"cmd": "drive", "l": 100, "r": -100}))


if __name__ == "__main__":
    unittest.main()
