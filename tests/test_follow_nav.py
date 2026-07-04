"""Tests for pi/follow_nav.py — pure steering logic, no hardware."""
from __future__ import annotations

from follow_nav import ble_should_turn_in_place, steer_around_obstacle


class TestSteerAroundObstacle:
    def test_non_forward_unchanged(self):
        assert steer_around_obstacle("left", forward_blocked=True) == "left"
        assert steer_around_obstacle("right", forward_blocked=True) == "right"
        assert steer_around_obstacle("backward", forward_blocked=True) == "backward"
        assert steer_around_obstacle("stop", forward_blocked=True) == "stop"

    def test_forward_not_blocked_unchanged(self):
        assert steer_around_obstacle("forward", forward_blocked=False) == "forward"

    def test_forward_blocked_person_left(self):
        result = steer_around_obstacle("forward", forward_blocked=True, person_cx=0.2)
        assert result == "left"

    def test_forward_blocked_person_right(self):
        result = steer_around_obstacle("forward", forward_blocked=True, person_cx=0.8)
        assert result == "right"

    def test_forward_blocked_person_centre_uses_default(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, person_cx=0.50, default_avoid="right"
        )
        assert result == "right"

    def test_forward_blocked_no_person_ble_left(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, ble_turn="left"
        )
        assert result == "left"

    def test_forward_blocked_no_person_ble_right(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, ble_turn="right"
        )
        assert result == "right"

    def test_forward_blocked_no_hints_uses_default(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, default_avoid="right"
        )
        assert result == "right"

    def test_forward_blocked_no_hints_default_left(self):
        result = steer_around_obstacle("forward", forward_blocked=True)
        assert result == "left"  # built-in default_avoid="left"

    def test_invalid_default_avoid_falls_back_to_left(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, default_avoid="straight"
        )
        assert result == "left"

    def test_person_cx_boundary_at_0_45(self):
        # Exactly 0.45 is not < 0.45, so not left; not > 0.55, so falls to default
        result = steer_around_obstacle(
            "forward", forward_blocked=True, person_cx=0.45, default_avoid="right"
        )
        assert result == "right"

    def test_person_cx_boundary_at_0_55(self):
        result = steer_around_obstacle(
            "forward", forward_blocked=True, person_cx=0.55, default_avoid="left"
        )
        assert result == "left"


class TestBleShouldTurnInPlace:
    def test_falling_rssi_returns_true(self):
        # rssi[-1] - rssi[-3] = -70 - (-65) = -5 < -2 → should turn
        history = [-65, -67, -68, -70]
        assert ble_should_turn_in_place(history) is True

    def test_stable_rssi_returns_false(self):
        history = [-65, -65, -66, -65]
        assert ble_should_turn_in_place(history) is False

    def test_rising_rssi_returns_false(self):
        # RSSI improving — no need to turn
        history = [-70, -68, -66, -65]
        assert ble_should_turn_in_place(history) is False

    def test_insufficient_samples_returns_false(self):
        assert ble_should_turn_in_place([-70, -72]) is False
        assert ble_should_turn_in_place([]) is False

    def test_exactly_min_samples(self):
        # min_samples=4: 4 samples — the check uses rssi[-1] - rssi[-3]
        # [-70] - [-68] = -2 → not < -2 (equal), so False
        history = [-65, -68, -68, -70]
        assert ble_should_turn_in_place(history) is False

    def test_custom_drop_dbm(self):
        # rssi[-1] - rssi[-3] = -70 - (-66) = -4 < -5 (custom threshold) → False
        history = [-65, -66, -68, -70]
        assert ble_should_turn_in_place(history, drop_dbm=5) is False

    def test_custom_drop_dbm_triggered(self):
        history = [-60, -61, -62, -70]  # drop of 9 > 2
        assert ble_should_turn_in_place(history, drop_dbm=2) is True
