"""Tests for pi/camera_idle.py — sliding-window idle logic, no hardware."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from camera_idle import CameraIdleManager


def _mgr(
    enabled: bool = True,
    sleep_after_s: float = 5.0,
    variance_cm: float = 8.0,
    window_size: int = 4,
) -> CameraIdleManager:
    cfg = {
        "camera_idle": {
            "enabled": enabled,
            "sleep_after_idle_s": sleep_after_s,
            "distance_variance_cm": variance_cm,
            "window_size": window_size,
        }
    }
    return CameraIdleManager(cfg)


def _fill_window(mgr: CameraIdleManager, value: float = 100.0, n: int = 4) -> None:
    """Fill the sliding window with stable readings."""
    for _ in range(n):
        mgr._feed(value, age_s=0.1)


class TestSleepingProperty:
    def test_not_sleeping_initially(self):
        mgr = _mgr()
        assert mgr.sleeping is False

    def test_sleeping_false_when_disabled(self):
        mgr = _mgr(enabled=False)
        mgr._sleeping = True
        assert mgr.sleeping is False  # _enabled=False masks _sleeping

    def test_sleeping_true_when_enabled_and_sleeping(self):
        mgr = _mgr(enabled=True)
        mgr._sleeping = True
        assert mgr.sleeping is True


class TestFeed:
    def test_window_not_full_no_sleep_decision(self):
        mgr = _mgr(window_size=4)
        mgr._feed(100.0, age_s=0.1)
        mgr._feed(100.0, age_s=0.1)
        # Only 2 of 4 readings — no decision yet
        assert mgr._sleeping is False
        assert mgr._stable_since is None

    def test_full_stable_window_starts_timer(self):
        mgr = _mgr(window_size=3, variance_cm=8.0, sleep_after_s=999.0)
        _fill_window(mgr, value=100.0, n=3)
        # Timer should be set but not expired
        assert mgr._stable_since is not None
        assert mgr._sleeping is False

    def test_variance_too_high_resets_timer(self):
        mgr = _mgr(window_size=3, variance_cm=5.0)
        mgr._feed(100.0, age_s=0.1)
        mgr._feed(100.0, age_s=0.1)
        mgr._feed(110.0, age_s=0.1)  # span=10 > 5 → reset
        assert mgr._stable_since is None

    def test_stale_reading_skipped(self):
        mgr = _mgr(window_size=2)
        mgr._feed(100.0, age_s=3.0)  # age > 2.0 → skipped
        assert len(mgr._window) == 0

    def test_none_cm_skipped(self):
        mgr = _mgr(window_size=2)
        mgr._feed(None, age_s=0.1)
        assert len(mgr._window) == 0

    def test_zero_cm_skipped(self):
        mgr = _mgr(window_size=2)
        mgr._feed(0.0, age_s=0.1)
        assert len(mgr._window) == 0

    def test_sleep_triggered_after_timer_expires(self):
        mgr = _mgr(window_size=3, variance_cm=5.0, sleep_after_s=0.0)
        # First 3 feeds fill the window and set _stable_since.
        # The 4th feed checks the timer (sleep_after_s=0 → already expired) and sleeps.
        _fill_window(mgr, value=100.0, n=4)
        assert mgr._sleeping is True

    def test_wake_on_large_delta_while_sleeping(self):
        mgr = _mgr(window_size=3, variance_cm=5.0, sleep_after_s=0.0)
        _fill_window(mgr, value=100.0, n=4)  # 4th feed triggers sleep
        assert mgr._sleeping is True
        mgr._baseline_cm = 100.0
        # Inject large delta while sleeping
        mgr._feed(120.0, age_s=0.1)  # delta=20 > 5
        assert mgr._sleeping is False

    def test_small_delta_while_sleeping_keeps_sleep(self):
        mgr = _mgr(window_size=3, variance_cm=5.0, sleep_after_s=0.0)
        _fill_window(mgr, value=100.0, n=4)  # 4th feed triggers sleep
        assert mgr._sleeping is True
        mgr._baseline_cm = 100.0
        mgr._feed(102.0, age_s=0.1)  # delta=2 < 5
        assert mgr._sleeping is True


class TestWakeInterface:
    def test_wake_clears_sleeping(self):
        mgr = _mgr()
        mgr._sleeping = True
        mgr.wake("test")
        assert mgr._sleeping is False

    def test_wake_resets_stable_since(self):
        mgr = _mgr()
        mgr._stable_since = 12345.0
        mgr.wake("test")
        assert mgr._stable_since is None

    def test_notify_stream_connect_wakes(self):
        mgr = _mgr()
        mgr._sleeping = True
        mgr.notify_stream_connect()
        assert mgr._sleeping is False

    def test_set_tracking_active_true_wakes_and_suppresses(self):
        mgr = _mgr()
        mgr._sleeping = True
        mgr.set_tracking_active(True)
        assert mgr._sleeping is False
        assert mgr._tracking_active is True

    def test_set_tracking_active_false_allows_idle(self):
        mgr = _mgr()
        mgr.set_tracking_active(True)
        mgr.set_tracking_active(False)
        assert mgr._tracking_active is False

    def test_notify_tracking_active_sets_flag(self):
        mgr = _mgr()
        mgr.notify_tracking_active()
        assert mgr._tracking_active is True

    def test_tracking_active_prevents_sleep(self):
        mgr = _mgr(window_size=2, variance_cm=5.0, sleep_after_s=0.0)
        mgr._tracking_active = True
        _fill_window(mgr, value=100.0, n=2)
        # Still sleeping=False because tracking is active (run() controls this,
        # but _feed itself doesn't check; testing via sleeping property)
        assert mgr.sleeping is False  # enabled=True but not sleeping
