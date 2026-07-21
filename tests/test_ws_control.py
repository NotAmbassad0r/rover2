"""build_telemetry() — WS payload construction, no real hardware/network."""
from __future__ import annotations

import unittest

from ws_control import build_telemetry


class FakeMegaPi:
    connected = True
    firmware_version = "rover2-basic-1.0.3"
    motors_ready = True
    port = "PORT_8"
    megapi_power_state = "ok"
    battery_estimate = "good"
    performance_ratio = 1.0
    megapi_samples_count = 5
    last_ultrasonic_cm = 42.0
    last_ultrasonic_age_s = 0.1
    ultrasonic_state = "ok"
    ultrasonic_fault_count = 0


class FakeBodyTracker:
    def __init__(self, configured_fps: float) -> None:
        self._configured_fps = configured_fps

    def get_state(self) -> dict:
        return {
            "available": True,
            "enabled": False,
            "detect_only": True,
            "hailo_ready": True,
            "person_detected": True,
            "person_bbox": [0.1, 0.2, 0.3, 0.4],
            "person_conf": 0.99,
            "person_cx": 0.3,
            "follow_decision": "NONE",
            "ble_active": False,
            "ble_follow_enabled": True,
            "follow_mode": "fused",
            "configured_fps": self._configured_fps,
        }


def _identity_public_ultrasonic_cm(value, age_s=None):
    return value


class BuildTelemetryTests(unittest.TestCase):
    def test_configured_fps_flows_from_body_tracker_into_payload(self) -> None:
        payload = build_telemetry(
            megapi=FakeMegaPi(),
            safety_monitor=None,
            start_monotonic=0.0,
            public_ultrasonic_cm=_identity_public_ultrasonic_cm,
            body_tracker=FakeBodyTracker(configured_fps=1.0),
        )
        self.assertIn("configured_fps", payload)
        self.assertEqual(payload["configured_fps"], 1.0)

    def test_configured_fps_reflects_a_different_rate(self) -> None:
        payload = build_telemetry(
            megapi=FakeMegaPi(),
            safety_monitor=None,
            start_monotonic=0.0,
            public_ultrasonic_cm=_identity_public_ultrasonic_cm,
            body_tracker=FakeBodyTracker(configured_fps=2.5),
        )
        self.assertEqual(payload["configured_fps"], 2.5)

    def test_configured_fps_absent_without_body_tracker(self) -> None:
        payload = build_telemetry(
            megapi=FakeMegaPi(),
            safety_monitor=None,
            start_monotonic=0.0,
            public_ultrasonic_cm=_identity_public_ultrasonic_cm,
            body_tracker=None,
        )
        self.assertNotIn("configured_fps", payload)


if __name__ == "__main__":
    unittest.main()
