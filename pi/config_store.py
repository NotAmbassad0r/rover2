"""Safe config tuning: validate, merge, persist config.yaml."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

# (type, min, max) — min/max None for str/bool
FieldSpec = tuple[str, Any, Any]

TUNING_SECTIONS: dict[str, dict[str, FieldSpec]] = {
    "body_tracker": {
        "confidence": ("float", 0.15, 0.95),
        "centre_zone": ("float", 0.10, 0.60),
        "target_bbox_width": ("float", 0.15, 0.70),
        "turn_speed": ("int", 30, 255),
        "forward_speed": ("int", 30, 255),
        "avoid_default": ("str", None, None),
        "frame_interval_s": ("float", 0.05, 0.50),
    },
    "safety": {
        "safe_distance_cm": ("int", 15, 120),
        "poll_interval_s": ("float", 0.2, 2.0),
        "initial_poll_delay_s": ("float", 0.5, 10.0),
    },
    "ble_tracker": {
        "rssi_track": ("int", -95, -50),
        "rssi_close": ("int", -80, -40),
        "search_speed": ("int", 20, 150),
        "fwd_speed": ("int", 20, 150),
        "beacon_uuid": ("str", None, None),
        "device_name": ("str", None, None),
    },
    "drive": {
        "default_speed": ("int", 30, 255),
        "max_speed": ("int", 50, 255),
    },
    "arm": {
        "max_speed": ("int", 50, 255),
        "invert": ("bool", None, None),
    },
    "websocket": {
        "telemetry_interval_s": ("float", 0.1, 2.0),
        "heartbeat_timeout_s": ("float", 2.0, 30.0),
    },
    "ultrasonic": {
        "poll_interval_s": ("float", 0.2, 2.0),
    },
    "agent": {
        "backend": ("str", None, None),
        "model": ("str", None, None),
        "ollama_base": ("str", None, None),
        "hailo_ollama_base": ("str", None, None),
        "ollama_timeout_s": ("float", 30.0, 300.0),
        "warmup_on_start": ("bool", None, None),
    },
}


def tuning_schema_public() -> dict[str, Any]:
    """Schema for UI and agent: sections, keys, types, ranges."""
    out: dict[str, Any] = {}
    for section, fields in TUNING_SECTIONS.items():
        out[section] = {
            k: {"type": spec[0], "min": spec[1], "max": spec[2]}
            for k, spec in fields.items()
        }
    return out


def _clamp(value: Any, lo: Any, hi: Any, typ: str) -> Any:
    if typ == "bool":
        return bool(value) if not isinstance(value, bool) else value
    if typ == "str":
        return str(value).strip()
    if typ == "int":
        return int(max(lo, min(hi, int(value))))
    if typ == "float":
        return float(max(lo, min(hi, float(value))))
    return value


def extract_tuning_patch(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate tuning body; raises ValueError on bad input."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    out: dict[str, dict[str, Any]] = {}
    for section, fields in TUNING_SECTIONS.items():
        src = body.get(section)
        if src is None:
            continue
        if not isinstance(src, dict):
            raise ValueError(f"{section} must be an object")
        section_out: dict[str, Any] = {}
        for key, (typ, lo, hi) in fields.items():
            if key not in src:
                continue
            raw = src[key]
            if typ == "str" and key == "avoid_default":
                val = str(raw).lower()
                if val not in ("left", "right"):
                    raise ValueError("avoid_default must be left or right")
                section_out[key] = val
            elif typ == "bool":
                section_out[key] = bool(raw)
            else:
                section_out[key] = _clamp(raw, lo, hi, typ)
        if section_out:
            out[section] = section_out
    if not out:
        raise ValueError("no recognized tuning fields")
    return out


def merge_tuning(config: dict[str, Any], patch: dict[str, dict[str, Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(config)
    for section, values in patch.items():
        merged.setdefault(section, {})
        if not isinstance(merged[section], dict):
            merged[section] = {}
        merged[section].update(values)
    return merged


def save_config(path: Path, config: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
