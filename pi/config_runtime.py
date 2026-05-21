"""Apply config patches to running ROVER2 subsystems."""

from __future__ import annotations

from typing import Any

from body_tracker import BodyTracker
from megapi import MegaPiBridge
from safety import SafetyMonitor
from ws_control import ControlHub


def apply_config_patch(
    patch: dict[str, dict[str, Any]],
    *,
    config: dict[str, Any],
    megapi: MegaPiBridge,
    safety_monitor: SafetyMonitor | None,
    body_tracker: BodyTracker | None,
    hub: ControlHub | None,
    rover_agent: Any | None = None,
) -> dict[str, Any]:
    """Apply validated tuning patch to runtime (config dict already merged)."""
    applied: dict[str, Any] = {}

    if safety_monitor is not None and "safety" in patch:
        sec = patch["safety"]
        if "safe_distance_cm" in sec:
            safety_monitor.set_safe_distance_cm(int(sec["safe_distance_cm"]))
        applied["safety"] = dict(sec)

    if body_tracker is not None:
        bt_patch: dict[str, Any] = {}
        if "body_tracker" in patch:
            bt_patch.update(patch["body_tracker"])
        if "ble_tracker" in patch:
            bt_patch.update(patch["ble_tracker"])
        if bt_patch:
            applied["body_tracker"] = body_tracker.apply_tuning(bt_patch)

    if "drive" in patch and hub is not None:
        d = patch["drive"]
        hub.apply_drive_settings(
            max_speed=d.get("max_speed"),
            default_speed=d.get("default_speed"),
        )
        applied["drive"] = dict(d)

    if "websocket" in patch and hub is not None:
        w = patch["websocket"]
        hub.apply_websocket_settings(
            telemetry_interval_s=w.get("telemetry_interval_s"),
            heartbeat_timeout_s=w.get("heartbeat_timeout_s"),
        )
        applied["websocket"] = dict(w)

    if "ultrasonic" in patch:
        u = patch["ultrasonic"]
        if "poll_interval_s" in u:
            megapi.set_poll_interval(float(u["poll_interval_s"]))
        applied["ultrasonic"] = dict(u)

    if "arm" in patch:
        applied["arm"] = dict(patch["arm"])

    if rover_agent is not None and "agent" in patch:
        ag = patch["agent"]
        if "model" in ag:
            rover_agent._model = str(ag["model"])
        if "ollama_base" in ag:
            rover_agent._base = str(ag["ollama_base"])
        applied["agent"] = dict(ag)

    return applied
