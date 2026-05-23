"""Unit tests for CameraIdleManager."""

from __future__ import annotations

import time
import unittest

from camera_idle import CameraIdleManager


def _mgr(
    *,
    enabled: bool = True,
    sleep_after_s: float = 10.0,
    variance_cm: float = 5.0,
    window_size: int = 4,
) -> CameraIdleManager:
    return CameraIdleManager({
        "camera_idle": {
            "enabled": enabled,
            "sleep_after_idle_s": sleep_after_s,
            "distance_variance_cm": variance_cm,
            "window_size": window_size,
        }
    })


def _feed_stable(mgr: CameraIdleManager, value: float = 100.0, n: int | None = None) -> None:
    """Fill window with a constant reading."""
    count = n if n is not None else mgr._window.maxlen  # type: ignore[arg-type]
    for _ in range(count):
        mgr._feed(value, 0.1)


class TestSleepDecision(unittest.TestCase):

    def test_window_must_be_full_before_any_decision(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        for _ in range(3):  # one short of full
            mgr._feed(100.0, 0.1)
        self.assertFalse(mgr.sleeping)

    def test_stable_window_triggers_sleep_after_timeout(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        _feed_stable(mgr, value=100.0)
        # stable_since was set on the 4th reading; feed one more to trip the timeout
        mgr._feed(100.0, 0.1)
        self.assertTrue(mgr.sleeping)

    def test_noisy_window_never_sleeps(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4, variance_cm=5.0)
        readings = [80.0, 100.0, 80.0, 100.0, 80.0, 100.0]
        for r in readings:
            mgr._feed(r, 0.1)
        self.assertFalse(mgr.sleeping)

    def test_variance_boundary_exact_is_stable(self) -> None:
        # span == variance_cm exactly → stable
        mgr = _mgr(sleep_after_s=0.0, window_size=4, variance_cm=5.0)
        for v in [100.0, 100.0, 105.0, 100.0, 100.0]:
            mgr._feed(v, 0.1)
        self.assertTrue(mgr.sleeping)

    def test_variance_boundary_one_over_is_not_stable(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4, variance_cm=5.0)
        for v in [100.0, 100.0, 105.1, 100.0, 100.0]:
            mgr._feed(v, 0.1)
        self.assertFalse(mgr.sleeping)

    def test_out_of_range_readings_count_as_stable(self) -> None:
        # Readings ≥ 400 cm (no echo) are capped to 400 and treated as stable open space
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        for v in [400.0, 450.0, 500.0, 400.0, 400.0]:
            mgr._feed(v, 0.1)
        self.assertTrue(mgr.sleeping)

    def test_negative_readings_ignored(self) -> None:
        # Negative = invalid sensor reading — skip, do not fill window
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        for _ in range(10):
            mgr._feed(-1.0, 0.1)
        self.assertFalse(mgr.sleeping)

    def test_stale_readings_ignored(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        for _ in range(10):
            mgr._feed(100.0, 3.0)  # age_s > 2.0 → ignored
        self.assertFalse(mgr.sleeping)

    def test_none_readings_ignored(self) -> None:
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        for _ in range(10):
            mgr._feed(None, 0.1)
        self.assertFalse(mgr.sleeping)

    def test_sleep_requires_continuous_stable_time(self) -> None:
        mgr = _mgr(sleep_after_s=5.0, window_size=4)
        _feed_stable(mgr, value=100.0)
        # stable_since was just set — not enough time has passed
        self.assertFalse(mgr.sleeping)

    def test_noise_resets_stable_timer(self) -> None:
        mgr = _mgr(sleep_after_s=2.0, window_size=4)
        _feed_stable(mgr, value=100.0)
        mgr._stable_since = time.monotonic() - 1.5  # almost there
        mgr._feed(150.0, 0.1)                        # noisy reading resets timer
        mgr._stable_since = time.monotonic() - 1.5  # advance time again
        mgr._feed(100.0, 0.1)                        # still not 2s
        self.assertFalse(mgr.sleeping)


class TestWake(unittest.TestCase):

    def _sleeping_mgr(self) -> CameraIdleManager:
        mgr = _mgr(sleep_after_s=0.0, window_size=4)
        _feed_stable(mgr)
        mgr._feed(100.0, 0.1)  # trip the sleep
        self.assertTrue(mgr.sleeping)
        return mgr

    def test_wake_clears_sleep(self) -> None:
        mgr = self._sleeping_mgr()
        mgr.wake("test")
        self.assertFalse(mgr.sleeping)

    def test_notify_tracking_active_wakes(self) -> None:
        mgr = self._sleeping_mgr()
        mgr.notify_tracking_active()
        self.assertFalse(mgr.sleeping)

    def test_notify_stream_connect_wakes(self) -> None:
        mgr = self._sleeping_mgr()
        mgr.notify_stream_connect()
        self.assertFalse(mgr.sleeping)

    def test_ultrasonic_delta_wakes(self) -> None:
        mgr = self._sleeping_mgr()
        baseline = mgr._baseline_cm or 100.0
        mgr._feed(baseline + 20.0, 0.1)  # large delta
        self.assertFalse(mgr.sleeping)

    def test_small_delta_stays_asleep(self) -> None:
        mgr = self._sleeping_mgr()
        baseline = mgr._baseline_cm or 100.0
        mgr._feed(baseline + 2.0, 0.1)  # within variance_cm=5
        self.assertTrue(mgr.sleeping)

    def test_wake_resets_stable_since(self) -> None:
        mgr = self._sleeping_mgr()
        mgr._stable_since = time.monotonic()  # set artificially
        mgr.wake("test")
        self.assertIsNone(mgr._stable_since)

    def test_can_re_sleep_after_wake(self) -> None:
        mgr = self._sleeping_mgr()
        mgr.wake("test")
        self.assertFalse(mgr.sleeping)
        # Feed stable again for long enough
        _feed_stable(mgr, value=100.0)
        mgr._feed(100.0, 0.1)
        self.assertTrue(mgr.sleeping)


class TestDisabled(unittest.TestCase):

    def test_disabled_never_reports_sleeping(self) -> None:
        mgr = _mgr(enabled=False, sleep_after_s=0.0, window_size=2)
        for _ in range(10):
            mgr._feed(100.0, 0.1)
        # Internal flag may be set but .sleeping returns False
        self.assertFalse(mgr.sleeping)

    def test_disabled_sleeping_property_masks_internal_state(self) -> None:
        mgr = _mgr(enabled=False, sleep_after_s=0.0, window_size=2)
        mgr._sleeping = True  # force internal state
        self.assertFalse(mgr.sleeping)

    def test_default_config_no_camera_idle_key(self) -> None:
        # Must not crash with missing camera_idle section
        mgr = CameraIdleManager({})
        mgr._feed(100.0, 0.1)
        self.assertFalse(mgr.sleeping)


if __name__ == "__main__":
    unittest.main()
