"""ControlHub._apply_drive() — motor command clamping (A4).

direction_map multipliers come from config.yaml (drive.direction_map) and
are only validated for shape, not magnitude, before reaching _apply_drive.
The scaled speed itself is clamped to ±max_speed, but the left/right values
actually sent to the firmware (mult * speed) were not re-clamped after the
multiplication — so a direction_map entry with |mult| > 1 could send an
out-of-range value.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from ws_control import ControlHub


def _make_hub(directions: dict, max_speed: int = 255) -> tuple[ControlHub, MagicMock]:
    megapi = MagicMock()
    hub = ControlHub(
        megapi=megapi,
        directions=directions,
        max_speed=max_speed,
        safety_monitor=None,
        start_monotonic=0.0,
        public_ultrasonic_cm=lambda v, age_s=None: v,
    )
    return hub, megapi


class TestApplyDriveClamping:
    def test_normal_direction_within_range(self):
        hub, megapi = _make_hub({"forward": (1, -1)})
        left, right = hub._apply_drive("forward", 1.0)
        assert left == 255
        assert right == -255
        megapi.drive.assert_called_once_with(255, -255)

    def test_out_of_range_positive_multiplier_is_clamped(self):
        # A misconfigured direction_map entry (e.g. a typo'd multiplier of 2
        # instead of 1) must not reach the serial link un-clamped.
        hub, megapi = _make_hub({"forward": (2, -2)})
        left, right = hub._apply_drive("forward", 1.0)
        assert left == 255
        assert right == -255
        megapi.drive.assert_called_once_with(255, -255)

    def test_out_of_range_negative_multiplier_is_clamped(self):
        hub, megapi = _make_hub({"forward": (-3, 3)})
        left, right = hub._apply_drive("forward", 1.0)
        assert left == -255
        assert right == 255

    def test_partial_speed_out_of_range_multiplier_is_clamped(self):
        hub, megapi = _make_hub({"forward": (2, -2)})
        left, right = hub._apply_drive("forward", 0.8)  # scale() -> 204; 2*204=408
        assert left == 255
        assert right == -255
