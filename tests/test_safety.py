"""Tests for pi/safety.py — obstacle gate logic with mocked megapi."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from safety import SafetyMonitor
from tests.mocks.fake_motor import FakeMotorSink


def _make_monitor(
    forward_signs: tuple[int, int] = (1, -1),
    safe_distance_cm: int = 40,
    last_ultrasonic_age_s: float | None = 0.1,
) -> tuple[SafetyMonitor, MagicMock]:
    fake_megapi = MagicMock()
    fake_megapi.send = FakeMotorSink()
    fake_megapi.on_message = None
    fake_megapi.last_ultrasonic_age_s = last_ultrasonic_age_s
    sm = SafetyMonitor(
        megapi=fake_megapi,
        forward_signs=forward_signs,
        safe_distance_cm=safe_distance_cm,
        poll_interval_s=0.5,
        initial_poll_delay_s=0.0,
    )
    return sm, fake_megapi


class TestIsForward:
    def test_matching_signs_is_forward(self):
        sm, _ = _make_monitor(forward_signs=(1, -1))
        assert sm._is_forward(170, -170) is True

    def test_inverted_signs_is_not_forward(self):
        sm, _ = _make_monitor(forward_signs=(1, -1))
        assert sm._is_forward(-170, 170) is False

    def test_zero_zero_is_not_forward(self):
        sm, _ = _make_monitor(forward_signs=(1, -1))
        assert sm._is_forward(0, 0) is False

    def test_arc_turn_one_zero_is_not_forward(self):
        sm, _ = _make_monitor(forward_signs=(1, -1))
        # Arc turn: only right motor
        assert sm._is_forward(0, -170) is False

    def test_forward_sign_zero_always_false(self):
        sm, _ = _make_monitor(forward_signs=(1, 0))
        assert sm._is_forward(100, 0) is False


class TestShouldBlock:
    def test_blocks_forward_near_obstacle(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 25
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is True

    def test_does_not_block_forward_beyond_threshold(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 60
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is False

    def test_does_not_block_when_distance_none(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = None
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is False

    def test_does_not_block_reverse(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 10
        cmd = {"cmd": "drive", "l": -170, "r": 170}
        assert sm._should_block(cmd) is False

    def test_does_not_block_non_drive_command(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 5
        cmd = {"cmd": "arm", "pwm": 100}
        assert sm._should_block(cmd) is False

    def test_blocks_exactly_at_distance_below_threshold(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 39  # < 40 → blocked
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is True

    def test_does_not_block_at_threshold(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm._distance_cm = 40  # not < 40
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is False


class TestGuardedSend:
    def _started_monitor(self) -> tuple[SafetyMonitor, FakeMotorSink]:
        fake_sink = FakeMotorSink()
        fake_megapi = MagicMock()
        fake_megapi.send = fake_sink
        fake_megapi.on_message = None
        fake_megapi.last_ultrasonic_age_s = 0.1
        sm = SafetyMonitor(
            megapi=fake_megapi,
            forward_signs=(1, -1),
            safe_distance_cm=40,
        )
        sm.start()
        sm._orig_send = fake_sink
        return sm, fake_sink

    def test_forward_near_obstacle_replaced_with_stop(self):
        sm, sink = self._started_monitor()
        sm._distance_cm = 20
        sm._guarded_send({"cmd": "drive", "l": 170, "r": -170})
        assert sink.last() == {"cmd": "drive", "l": 0, "r": 0}
        assert sm.forward_blocked is True

    def test_forward_safe_distance_passes_through(self):
        sm, sink = self._started_monitor()
        sm._distance_cm = 60
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        sm._guarded_send(cmd)
        assert sink.last() == cmd

    def test_reverse_passes_through_and_clears_block(self):
        sm, sink = self._started_monitor()
        sm._distance_cm = 20
        sm._forward_blocked = True
        reverse_cmd = {"cmd": "drive", "l": -170, "r": 170}
        sm._guarded_send(reverse_cmd)
        assert sink.last() == reverse_cmd
        assert sm.forward_blocked is False

    def test_non_drive_command_passes_through(self):
        sm, sink = self._started_monitor()
        sm._distance_cm = 5
        cmd = {"cmd": "arm", "pwm": 100}
        sm._guarded_send(cmd)
        assert sink.last() == cmd


class TestIngestUltrasonic:
    def test_valid_sensor_message_updates_distance(self):
        sm, _ = _make_monitor()
        sm._ingest_ultrasonic({"evt": "sensor", "ultrasonic_cm": 35})
        assert sm._distance_cm == 35

    def test_type_field_also_accepted(self):
        sm, _ = _make_monitor()
        sm._ingest_ultrasonic({"type": "sensor", "ultrasonic_cm": 50})
        assert sm._distance_cm == 50

    def test_non_sensor_message_ignored(self):
        sm, _ = _make_monitor()
        sm._distance_cm = 99
        sm._ingest_ultrasonic({"evt": "status", "ultrasonic_cm": 10})
        assert sm._distance_cm == 99

    def test_zero_distance_set_to_none(self):
        sm, _ = _make_monitor()
        sm._ingest_ultrasonic({"evt": "sensor", "ultrasonic_cm": 0})
        assert sm._distance_cm is None

    def test_negative_distance_set_to_none(self):
        sm, _ = _make_monitor()
        sm._ingest_ultrasonic({"evt": "sensor", "ultrasonic_cm": -5})
        assert sm._distance_cm is None

    def test_none_distance_set_to_none(self):
        sm, _ = _make_monitor()
        sm._ingest_ultrasonic({"evt": "sensor", "ultrasonic_cm": None})
        assert sm._distance_cm is None


class TestStaleness:
    """A1: a frozen/stale ultrasonic reading must not be trusted as 'clear' forever."""

    def test_stale_clear_reading_blocks_forward(self):
        # Sensor reported "clear" (200 cm) once, then the ultrasonic subsystem
        # stalled — no reading for 10s even though the serial link is alive.
        sm, _ = _make_monitor(safe_distance_cm=40, last_ultrasonic_age_s=10.0)
        sm._distance_cm = 200
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is True

    def test_fresh_clear_reading_does_not_block(self):
        sm, _ = _make_monitor(safe_distance_cm=40, last_ultrasonic_age_s=0.1)
        sm._distance_cm = 200
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is False

    def test_missing_age_blocks_forward(self):
        # last_ultrasonic_age_s is None until the very first reading ever
        # arrives — but here _distance_cm is already set, an inconsistent
        # state that should be treated as untrustworthy, not "clear forever".
        sm, _ = _make_monitor(safe_distance_cm=40, last_ultrasonic_age_s=None)
        sm._distance_cm = 200
        cmd = {"cmd": "drive", "l": 170, "r": -170}
        assert sm._should_block(cmd) is True

    def test_stale_reading_does_not_affect_reverse(self):
        sm, _ = _make_monitor(safe_distance_cm=40, last_ultrasonic_age_s=10.0)
        sm._distance_cm = 200
        cmd = {"cmd": "drive", "l": -170, "r": 170}
        assert sm._should_block(cmd) is False


class TestMiscPublicMethods:
    def test_set_safe_distance_updates_threshold(self):
        sm, _ = _make_monitor(safe_distance_cm=40)
        sm.set_safe_distance_cm(60)
        assert sm.safe_distance_cm == 60

    def test_clear_block_resets_flag(self):
        sm, _ = _make_monitor()
        sm._forward_blocked = True
        sm.clear_block()
        assert sm.forward_blocked is False

    def test_distance_cm_property(self):
        sm, _ = _make_monitor()
        sm._distance_cm = 77
        assert sm.distance_cm == 77
