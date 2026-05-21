"""Unit tests for follow obstacle / BLE steering helpers."""

from __future__ import annotations

import unittest

from follow_nav import ble_should_turn_in_place, steer_around_obstacle


class SteerAroundObstacleTests(unittest.TestCase):
    def test_passes_through_when_clear(self) -> None:
        self.assertEqual(
            steer_around_obstacle("forward", forward_blocked=False),
            "forward",
        )
        self.assertEqual(
            steer_around_obstacle("left", forward_blocked=True),
            "left",
        )

    def test_turns_toward_person_side(self) -> None:
        self.assertEqual(
            steer_around_obstacle(
                "forward", forward_blocked=True, person_cx=0.2,
            ),
            "left",
        )
        self.assertEqual(
            steer_around_obstacle(
                "forward", forward_blocked=True, person_cx=0.8,
            ),
            "right",
        )

    def test_ble_turn_when_person_centred(self) -> None:
        self.assertEqual(
            steer_around_obstacle(
                "forward",
                forward_blocked=True,
                person_cx=0.5,
                ble_turn="right",
            ),
            "right",
        )

    def test_default_avoid_side(self) -> None:
        self.assertEqual(
            steer_around_obstacle("forward", forward_blocked=True, default_avoid="right"),
            "right",
        )


class BleTurnInPlaceTests(unittest.TestCase):
    def test_detects_falling_rssi(self) -> None:
        self.assertTrue(ble_should_turn_in_place([-70, -72, -74, -78]))

    def test_ignores_short_history(self) -> None:
        self.assertFalse(ble_should_turn_in_place([-70, -72]))

    def test_ignores_improving_rssi(self) -> None:
        self.assertFalse(ble_should_turn_in_place([-80, -75, -72, -68]))


if __name__ == "__main__":
    unittest.main()
