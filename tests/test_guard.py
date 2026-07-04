"""Tests for pi/guard.py — state machine logic with mocked BLE and body_tracker."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from guard import GuardController, _iso_wall, _iso_now
from tests.mocks.fake_ble import FakeBLETracker


def _make_guard(
    enabled: bool = True,
    rssi_present: int = -70,
    rssi_return: int = -65,
    leaving_debounce_s: float = 0.0,
    intruder_confidence: float = 0.55,
    alert_hold_s: float = 30.0,
    ble_rssi: int | None = -60,
) -> GuardController:
    cfg = {
        "guard": {
            "enabled": enabled,
            "rssi_present_threshold": rssi_present,
            "rssi_return_threshold": rssi_return,
            "rssi_leaving_debounce_s": leaving_debounce_s,
            "intruder_confidence": intruder_confidence,
            "alert_hold_s": alert_hold_s,
            "speak_on_state_change": False,  # suppress TTS in tests
            "ha_base_url": "",               # disables webhook HTTP calls
            "ha_token": "",
            "webhook_intruder_id": "rover_intruder",
            "webhook_armed_id": "rover_armed",
            "webhook_disarmed_id": "rover_disarmed",
        }
    }
    ble = FakeBLETracker(rssi=ble_rssi)
    body = MagicMock()
    body.set_detection_callback = MagicMock()
    gc = GuardController(config=cfg, ble_tracker=ble, body_tracker=body)
    gc._speak_fn = None  # no audio in tests
    return gc


class TestTransitionAndState:
    def test_initial_state_is_disarmed(self):
        gc = _make_guard()
        assert gc.get_state() == GuardController.DISARMED

    async def test_manual_override_arm(self):
        gc = _make_guard()
        state = await gc.manual_override("arm")
        assert state == GuardController.ARMED

    async def test_manual_override_disarm(self):
        gc = _make_guard()
        await gc.manual_override("arm")
        state = await gc.manual_override("disarm")
        assert state == GuardController.DISARMED

    def test_transition_same_state_no_change(self):
        gc = _make_guard()
        gc._transition(GuardController.DISARMED)  # already DISARMED
        assert gc.get_state() == GuardController.DISARMED

    async def test_manual_override_enable(self):
        gc = _make_guard(enabled=False)
        await gc.manual_override("enable")
        assert gc.enabled is True

    async def test_manual_override_disable(self):
        gc = _make_guard(enabled=True)
        await gc.manual_override("disable")
        assert gc.enabled is False


class TestTransitionSet:
    """Tests for set_enabled/disable which call _transition → ensure_future."""

    async def test_set_enabled_false_transitions_to_disarmed(self):
        gc = _make_guard()
        gc._state = GuardController.ARMED
        gc.set_enabled(False)
        assert gc.get_state() == GuardController.DISARMED
        assert gc.enabled is False


class TestOnPersonDetected:
    def _armed_guard(self) -> GuardController:
        gc = _make_guard(intruder_confidence=0.55, ble_rssi=None)
        gc._state = GuardController.ARMED
        gc._body.set_detection_callback = MagicMock()
        return gc

    async def test_high_confidence_triggers_alert(self):
        gc = self._armed_guard()
        gc._handle_intruder(0.80, (0.1, 0.2, 0.9, 0.8))
        assert gc.get_state() == GuardController.ALERT
        assert gc._detections == 1

    def test_low_confidence_no_state_change(self):
        gc = self._armed_guard()
        gc._on_person_detected(0.30, (0.1, 0.2, 0.9, 0.8))
        assert gc.get_state() == GuardController.ARMED

    def test_detection_when_disarmed_ignored(self):
        gc = _make_guard()
        gc._on_person_detected(0.90, (0.1, 0.2, 0.9, 0.8))
        assert gc.get_state() == GuardController.DISARMED

    def test_detection_when_beacon_present_ignored(self):
        # RSSI -60 >= rssi_return -65 → beacon present → no alert
        gc = _make_guard(rssi_return=-65, ble_rssi=-60)
        gc._state = GuardController.ARMED
        gc._on_person_detected(0.90, (0.1, 0.2, 0.9, 0.8))
        assert gc.get_state() == GuardController.ARMED

    async def test_detection_increments_counter(self):
        gc = self._armed_guard()
        gc._handle_intruder(0.80, (0.1, 0.2, 0.9, 0.8))
        gc._state = GuardController.ARMED  # reset for second detection
        gc._handle_intruder(0.75, (0.1, 0.2, 0.9, 0.8))
        assert gc._detections == 2


class TestGetStats:
    def test_get_stats_has_expected_keys(self):
        gc = _make_guard()
        stats = gc.get_stats()
        expected = {
            "guard_enabled", "state", "beacon_rssi",
            "armed_since", "detections_this_session",
            "last_detection", "last_webhook_ok", "last_webhook_at",
        }
        assert expected.issubset(stats.keys())

    def test_get_stats_reflects_enabled(self):
        gc = _make_guard(enabled=True)
        assert gc.get_stats()["guard_enabled"] is True


class TestHelperFunctions:
    def test_iso_wall_none_returns_none(self):
        assert _iso_wall(None) is None

    def test_iso_wall_returns_iso_string(self):
        ts = 1700000000.0
        result = _iso_wall(ts)
        assert result is not None
        assert "Z" in result

    def test_iso_now_returns_string(self):
        result = _iso_now()
        assert isinstance(result, str)
        assert "Z" in result
