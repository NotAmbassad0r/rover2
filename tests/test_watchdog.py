"""Tests for pi/watchdog.py — rate-limit and alert management logic, no hardware."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import watchdog as _wd_module
from watchdog import Watchdog, _MAX_RETRIES_PER_CHECK, _RETRY_WINDOW_S


@pytest.fixture(autouse=True)
def _patch_log_path(tmp_path, monkeypatch):
    """Redirect watchdog log to tmp_path — avoids /opt/rover2 permission errors."""
    monkeypatch.setattr(_wd_module, "_LOG_PATH", tmp_path / "watchdog.log")


def _make_watchdog(
    max_actions: int = 3,
    action_window_s: float = 3600.0,
) -> Watchdog:
    config = {
        "watchdog": {
            "enabled": True,
            "interval_s": 60,
            "memory_warn_mb": 420,
            "memory_critical_mb": 500,
            "temp_warn_c": 78,
            "temp_critical_c": 85,
            "action_window_s": action_window_s,
            "max_actions": max_actions,
            "ollama_idle_stop_minutes": 2,
            "cpu_sustained_percent": 90,
            "cpu_sustained_seconds": 60,
            "disk_warn_gb": 2.0,
            "disk_critical_gb": 0.5,
            "cert_warn_days": 30,
            "cert_critical_days": 7,
            "temp_follow_disable_c": 85.0,
        }
    }
    return Watchdog(config)


class TestGlobalActionRateLimiting:
    def test_can_act_initially(self):
        wd = _make_watchdog(max_actions=3)
        assert wd._can_act() is True

    def test_can_act_false_after_limit_reached(self):
        wd = _make_watchdog(max_actions=3)
        for _ in range(3):
            wd._last_action_desc = "test"
            wd._record_action()
        assert wd._can_act() is False
        assert wd._limit_reached is True

    def test_actions_used_count(self):
        wd = _make_watchdog(max_actions=5)
        wd._last_action_desc = "a"
        wd._record_action()
        wd._last_action_desc = "b"
        wd._record_action()
        assert wd.actions_used == 2

    def test_reset_actions_clears_limit(self):
        wd = _make_watchdog(max_actions=2)
        wd._last_action_desc = "x"
        wd._record_action()
        wd._last_action_desc = "y"
        wd._record_action()
        assert wd._limit_reached is True
        wd.reset_actions()
        assert wd._limit_reached is False
        assert wd._can_act() is True
        assert wd.actions_used == 0

    def test_reset_clears_persistent_alerts(self):
        wd = _make_watchdog()
        wd._emit_alert("test_key", "warning", "some alert")
        assert len(wd.get_persistent_alerts()) == 1
        wd.reset_actions()
        assert wd.get_persistent_alerts() == []

    def test_prune_window_removes_expired_actions(self):
        wd = _make_watchdog(max_actions=3, action_window_s=1.0)
        wd._last_action_desc = "old"
        wd._record_action()
        # Force the timestamp to be old
        wd._actions = [time.monotonic() - 2.0]  # expired
        wd._limit_reached = True
        wd._prune_window()
        wd._limit_reached = False
        assert wd._can_act() is True


class TestPerCheckRateLimiting:
    def test_can_act_for_key_initially(self):
        wd = _make_watchdog()
        assert wd._can_act_for("memory") is True

    def test_exhausted_after_max_retries(self):
        wd = _make_watchdog()
        for _ in range(_MAX_RETRIES_PER_CHECK):
            wd._record_attempt("memory")
        assert wd._can_act_for("memory") is False

    def test_exhausting_emits_persistent_alert(self):
        wd = _make_watchdog()
        for _ in range(_MAX_RETRIES_PER_CHECK):
            wd._record_attempt("memory")
        alerts = wd.get_persistent_alerts()
        assert any("memory" in a["metric"] for a in alerts)

    def test_different_keys_are_independent(self):
        wd = _make_watchdog()
        for _ in range(_MAX_RETRIES_PER_CHECK):
            wd._record_attempt("disk")
        assert wd._can_act_for("memory") is True

    def test_attempt_timestamps_pruned_after_window(self):
        wd = _make_watchdog()
        # Record old attempt (beyond retry window)
        wd._retry_ts["memory"] = [time.monotonic() - _RETRY_WINDOW_S - 1.0]
        assert wd._can_act_for("memory") is True


class TestAlertManagement:
    def test_emit_alert_adds_entry(self):
        wd = _make_watchdog()
        wd._emit_alert("disk_low", "warning", "disk is low")
        alerts = wd.get_persistent_alerts()
        assert len(alerts) == 1
        assert alerts[0]["metric"] == "watchdog_disk_low"
        assert alerts[0]["severity"] == "warning"
        assert "disk is low" in alerts[0]["msg"]

    def test_emit_alert_replaces_same_key(self):
        wd = _make_watchdog()
        wd._emit_alert("cpu", "warning", "first")
        wd._emit_alert("cpu", "critical", "second")
        alerts = wd.get_persistent_alerts()
        assert len([a for a in alerts if a["metric"] == "watchdog_cpu"]) == 1
        assert alerts[-1]["severity"] == "critical"

    def test_clear_alert_removes_by_key(self):
        wd = _make_watchdog()
        wd._emit_alert("foo", "warning", "bar")
        wd._clear_alert("foo")
        alerts = wd.get_persistent_alerts()
        assert not any(a["metric"] == "watchdog_foo" for a in alerts)

    def test_clear_alert_no_op_on_missing_key(self):
        wd = _make_watchdog()
        wd._clear_alert("nonexistent")  # should not raise
        assert wd.get_persistent_alerts() == []

    def test_get_persistent_alerts_returns_copy(self):
        wd = _make_watchdog()
        wd._emit_alert("k", "warning", "msg")
        copy1 = wd.get_persistent_alerts()
        copy1.clear()
        assert len(wd.get_persistent_alerts()) == 1

    def test_multiple_alerts_different_keys(self):
        wd = _make_watchdog()
        wd._emit_alert("mem", "warning", "mem high")
        wd._emit_alert("disk", "critical", "disk full")
        alerts = wd.get_persistent_alerts()
        assert len(alerts) == 2

    def test_alert_has_required_fields(self):
        wd = _make_watchdog()
        wd._emit_alert("test_alert", "warning", "test message", action_taken="none")
        alert = wd.get_persistent_alerts()[0]
        for field in ("metric", "severity", "msg", "ts", "source", "action_taken", "timestamp"):
            assert field in alert, f"Missing field: {field}"
        assert alert["source"] == "watchdog"

    def test_ok_property_reflects_limit(self):
        wd = _make_watchdog(max_actions=1)
        assert wd.ok is True
        wd._last_action_desc = "x"
        wd._record_action()
        assert wd.ok is False

    def test_action_history_tracks_last_10(self):
        wd = _make_watchdog(max_actions=100)
        for i in range(12):
            wd._last_action_desc = f"action-{i}"
            wd._record_action()
        history = wd.action_history
        assert len(history) <= 10
        assert history[-1]["desc"] == "action-11"
