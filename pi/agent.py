"""ROVER2 agentic AI — Ollama + tool use for autonomous diagnosis and fixes.

Add new capabilities here:
  1. Add a tool definition to _READ_TOOLS or _DANGEROUS_TOOLS.
  2. Add the matching case to _run_tool().
  3. The system prompt already instructs the model to call tools first.
  No other changes needed — the UI reads the tool list dynamically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

from agent_knowledge import ROVER2_KNOWLEDGE
from agent_solutions import build_solutions
from ha_client import HAClient

logger = logging.getLogger(__name__)

# ── Tool definitions ──────────────────────────────────────────────────────────
# Read-only / safe — model calls these autonomously.

_READ_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Current robot status: websocket, serial, motors, camera, safety, tracking, BLE, uptime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_processes",
            "description": "Top 12 processes by CPU right now with PID, name, full command, cpu_pct, mem_mb. "
                           "Always call this first when diagnosing high CPU or unknown load.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diagnostics",
            "description": "Full system diagnostics: CPU%, temperature, RAM, disk, network rates, rover2-api process stats, all service checks.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Last N log lines from rover2-api (the main robot service).",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Lines (max 80)", "default": 40},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_journal",
            "description": "Read systemd journal for any named service. "
                           "Use for: rover-camera, ollama, rover2-api, cpu-governor, bluetooth, NetworkManager, ssh.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "systemd service name (without .service)"},
                    "lines":   {"type": "integer", "default": 40},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "Status (active/inactive/failed/enabled/disabled) for all rover-related services: "
                           "rover2-api, rover-camera, ollama, cpu-governor, bluetooth, NetworkManager, ssh, "
                           "rover2-powerbank-keepalive, rover2-virtual-usb-dongle, hailo-ollama.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric",
            "description": "Historical time-series for one metric. "
                           "Metric names: cpu_percent, temp_cpu_thermal, ram_percent, ram_used_mb, "
                           "process_memory_mb, process_cpu_percent, ultrasonic_cm, ble_rssi, "
                           "follow_state, person_detected, safety_blocked, disk_percent, "
                           "net_eth0_tx_bps, net_eth0_rx_bps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric":  {"type": "string"},
                    "minutes": {"type": "integer", "default": 15},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wifi_info",
            "description": "WiFi connection: SSID, signal strength, IP addresses, default gateway, link details.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_disk_details",
            "description": "Disk usage: per-partition stats (total/used/free GB, %) plus du -sh for /opt/rover2, /opt/rover, /var/log, /tmp.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ping_host",
            "description": "Ping a host 3 times and return reachability, packet loss, RTT. "
                           "Use to diagnose network issues. Default host is the Pi's gateway.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "default": "192.168.70.1"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_hailo",
            "description": "Hailo AI chip status: hailo_platform installed, HEF files present, "
                           "body_tracker ready, VLM engine status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_health",
            "description": "Camera service health: is rover-camera streaming, last frame age, HTTP status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_db_stats",
            "description": "Metrics database: SQLite file size, number of tracked metrics, latest sample values.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_robot_capabilities",
            "description": "Full list of robot features, supported modes, and available API endpoints. "
                           "Use this when asked what the robot can do.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": "Current active threshold alerts: high temperature, high CPU, high RAM, low disk. "
                           "Returns empty list when everything is within safe limits.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_parameters",
            "description": "All tunable config parameters with types and min/max. Call before set_parameters.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "Current config.yaml (secrets masked).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_parameters",
            "description": "Change one or more parameters and save to config.yaml (live apply). "
                           "Use list_parameters for valid keys. "
                           "Example patch: {body_tracker: {turn_speed: 90}, safety: {safe_distance_cm: 35}}",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "object",
                        "description": "section name -> {key: value}",
                    },
                },
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "monitor_snapshot",
            "description": "One-shot health: robot status + full diagnostics (CPU, temp, RAM, disk, network) + top processes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hardware_reference",
            "description": "ROVER2 hardware/software reference: ports, IPs, services, BLE, Hailo, power notes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_camera",
            "description": "VLM describes what the camera sees. Optional custom prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "default": "Describe what you see in front of the robot."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnostics",
            "description": "Force fresh diagnostics scan (same as RUN DIAGNOSTICS in web UI).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "description": "Live status of the 5 key services (rover2-api, rover-camera, "
                           "rover2-virtual-usb-dongle, rover2-powerbank-keepalive, hailo-ollama) "
                           "plus current temperature, throttle state, and CPU frequency.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_thermal_status",
            "description": "Current CPU temperature, throttle state (current + since-boot), "
                           "and CPU frequency in MHz. Use when diagnosing heat, throttling, "
                           "or frequency scaling issues.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_robot_mode",
            "description": "Change the robot's operating mode: "
                           "follow (camera YOLO follow), detect (detect-only no movement), off (idle).",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["follow", "detect", "off"]},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wave_arm",
            "description": "Wave the robot arm as a greeting gesture. Use when greeted or when owner says hello, hi, or wave.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_diagnostics",
            "description": "Full system snapshot: robot status + CPU/temp/RAM/disk/network + top processes. "
                           "Use for 'how are you', 'system check', 'run diagnostics', 'full check'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hailo_status",
            "description": "Hailo AI chip and body tracker status: platform available, HEF loaded, "
                           "body_tracker ready, VLM engine status, current temperature.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak_diagnostics_summary",
            "description": "Fetch live system status and services, return compact spoken summary. "
                           "Use for 'how are you', 'status check', 'all good?', 'are you okay?'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_fix",
            "description": "Fetch current diagnostics and propose or apply fixes. "
                           "Safe fixes (rover-camera restart) applied automatically. "
                           "Risky fixes (rover2-api restart, reboot, config changes) returned as proposals. "
                           "Use for 'fix it', 'what is wrong', 'any problems'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_follow_mode",
            "description": "Enable, disable, or change the person-follow mode. "
                           "enable=start camera YOLO follow. disable=stop all follow. "
                           "detect=detect-only no movement. fused=BLE+camera fused follow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["enable", "disable", "camera", "fused", "detect"],
                    },
                },
                "required": ["action"],
            },
        },
    },
]

# ── Home Assistant tools (appended when HA_TOKEN + HA_URL are in env) ─────────
_HA_READ_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ha_get_status",
            "description": (
                "Get Home Assistant status — all lights, switches, sensors, "
                "temperature readings, and media players."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_temperatures",
            "description": "Get all room temperatures from Home Assistant sensors.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
_HA_DANGEROUS_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ha_toggle",
            "description": (
                "Toggle a Home Assistant entity on or off. "
                "Use for lights, switches, power strips, and plugs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "HA entity_id, e.g. light.office_go or switch.hue_smart_plug_1",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
]

# Dangerous — require explicit user confirmation via the Confirm button.
_DANGEROUS_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": "Restart rover2-api.service. Use only when there is clear evidence the service is stuck or crashed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_service",
            "description": "Stop a named service. Useful for stopping rogue or unnecessary services (e.g. rover-camera v1 if it's consuming CPU). "
                           "Allowed: rover-camera, ollama, rover2-powerbank-keepalive, rover2-virtual-usb-dongle, hailo-ollama, bluetooth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["service", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_service",
            "description": "Start a named service that is currently stopped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["service", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reboot_pi",
            "description": "Reboot the Raspberry Pi. Absolute last resort only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]

_HA_ENABLED = bool(os.environ.get("HA_TOKEN") and os.environ.get("HA_URL"))
_ALL_TOOLS = (
    _READ_TOOLS + _HA_READ_TOOLS
    + _DANGEROUS_TOOLS + _HA_DANGEROUS_TOOLS
) if _HA_ENABLED else _READ_TOOLS + _DANGEROUS_TOOLS
_DANGEROUS_NAMES = {t["function"]["name"] for t in _DANGEROUS_TOOLS + _HA_DANGEROUS_TOOLS}

# Reduced tool set for CPU Ollama — 38-tool context exceeds Pi 5 ARM capacity (~150s warm).
# Queries covered by _FAST_PATTERNS (temps, disk, wifi, services, hailo, capabilities) are
# handled before the LLM is ever called, so those tools are omitted here.
_CPU_TOOL_NAMES = {
    # Monitoring — single call covers status + diagnostics + top processes
    "monitor_snapshot",
    # Diagnosis and log investigation
    "suggest_fix", "get_logs", "get_hardware_reference",
    # Config management
    "list_parameters", "set_parameters",
    # Robot control
    "control_follow_mode", "describe_camera",
    # Dangerous ops (guarded by UI Confirm button)
    "restart_service", "stop_service", "start_service", "reboot_pi",
    # HA — only present in _ALL_TOOLS when _HA_ENABLED
    "ha_get_status", "ha_toggle",
}
_CPU_TOOLS = [t for t in _ALL_TOOLS if t["function"]["name"] in _CPU_TOOL_NAMES]

_SYSTEM_PROMPT = """You are ROVER2's onboard engineer: monitor hardware and software, change any allowed parameter, answer related questions.

WORKFLOW:
1. Monitoring (CPU, temp, RAM, disk, serial, camera, follow, BLE): call monitor_snapshot().
2. Change settings: call list_parameters() then set_parameters(patch) with one or more sections.
3. Hardware/software facts (ports, IPs, wiring, services): call get_hardware_reference().
4. Vision: describe_camera() for what the camera sees.
5. Robot control: control_follow_mode(enable|disable|camera|fused|detect).

RULES:
- ALWAYS use tools for live data. Never invent temperatures, CPU %, or RSSI.
- Temperature: read from monitor_snapshot result — warn above 80 C, critical above 85 C.
- CPU high: check top processes in monitor_snapshot result.
- After finding a fixable config issue, call set_parameters — do not only suggest edits.
- Dangerous actions (restart_service, reboot_pi, stop_service): use proposal only.
- Plain text, short paragraphs. No markdown.
- Always end with concrete suggested fixes (what to stop, restart, or change)."""

_MAX_TOOL_ROUNDS = 4
_DEFAULT_OLLAMA_TIMEOUT_S = 180

_SPOKEN_CPU_BASE = "http://127.0.0.1:11434"
_SPOKEN_MODEL = "llama3.2:1b"
_SPOKEN_TIMEOUT_S = 45.0   # 14s per round with minimal tool set; keep-alive prevents cold-start waits
_KEEPALIVE_INTERVAL_S = 240  # 4 minutes — keeps model loaded between spoken requests

# Conversation routing — 3b for complex queries
_CPU_3B_MODEL   = "llama3.2:3b"
_CPU_3B_TIMEOUT_S = 90.0  # 3b is larger; 90s sufficient with loaded model

# hailo-ollama supports /api/chat when message content has no newlines.
# /api/generate with any prompt fails to anchor the ROVER persona in qwen2.5-instruct:1.5b
# because the instruction-tuned model ignores raw-prompt few-shot when not in ChatML format.
# Solution: /api/chat with flat (no-newline) system prompt + 3 few-shot prior turns.
_ROVER_HAILO_SYSTEM = (
    "You are ROVER, the AI of an autonomous robot. "
    "Dry sardonic British wit. Never say certainly, absolutely, I cannot, or I am unable. "
    "Call owner sir. 2-3 sentences. No preamble, no lists."
)
_ROVER_HAILO_FEW_SHOT: list[dict] = [
    {"role": "user",      "content": "Tell me a joke."},
    {"role": "assistant", "content": "Why do programmers prefer dark mode? Because light attracts bugs, sir. I trust that suffices."},
    {"role": "user",      "content": "Are you okay?"},
    {"role": "assistant", "content": "All systems nominal. Though you did just ask a robot how it is feeling, sir."},
    {"role": "user",      "content": "What can you do?"},
    {"role": "assistant", "content": "I can see, move, follow people, and monitor my own vitals, sir. I can also detect when a question is rhetorical, though I answer regardless."},
]

_TOOLS_ONLY_REPLY = (
    "Pi is in bridge mode (A32 ↔ MegaPi): no CPU LLM. "
    "Use quick actions (TEMPS, CHECK LOGS, DIAG ASK AI) or enable hailo-ollama on the AI HAT+ "
    "(./scripts/setup_ai_on_hailo.sh). Vision/follow already run on the HAT."
)

# Fast-path spoken command patterns: (regex, tool_name, args_dict)
# Matched before any LLM call — direct tool execution + hailo voice response.
_SPOKEN_FAST_PATTERNS: list[tuple[str, str, dict]] = [
    (r"\bhell[oa]\b|\bhi\b|\bhey\b|\bwave\b",
     "wave_arm", {}),
    (r"how are you|are you okay|are you well|how.{0,10} doing|system (check|status)|all good",
     "speak_diagnostics_summary", {}),
    (r"follow me|come here|start follow|track me|start tracking|begin follow",
     "control_follow_mode", {"action": "enable"}),
    (r"fused follow|ble.{0,10}follow|bluetooth.{0,10}follow",
     "control_follow_mode", {"action": "fused"}),
    (r"detect only|just detect|watch (but|don|without moving)",
     "control_follow_mode", {"action": "detect"}),
    (r"stop follow|stop (coming|tracking)|stay( here)?|don.{0,3}t follow|cease follow",
     "control_follow_mode", {"action": "disable"}),
    (r"what do you see|describe.*see|look.*ahead|what.s in front|camera view|what.{0,10}ahead",
     "describe_camera", {}),
    (r"\bdiagnostics?\b|run diagnostics?|one diagnostics?|show diagnostics?|system diagnostics?|give diagnostics?|tell diagnostics?|full check|health check|run a check|full scan|detailed (check|status)",
     "get_full_diagnostics", {}),
    (r"fix it|what.{0,8}wrong|any problem|suggest fix|anything wrong|what.s broken|is anything",
     "suggest_fix", {}),
    (r"\bhailo\b|ai chip|neural processor|inference chip",
     "get_hailo_status", {}),
    (r"current status|status report|how.{0,10}system",
     "speak_diagnostics_summary", {}),
    (r"internal temp(erature)?|cpu temp(erature)?|pi temp(erature)?|system temp(erature)?|how hot|temperature of the (pi|rover)|rover temp(erature)?",
     "get_temperature_status", {}),
    # Home Assistant — room temperatures
    (r"room temp(erature)?|temperature (in|of|at)\b|how warm|how cold|what.{0,10}temp|"
     r"house temp(erature)?|flat temp(erature)?",
     "ha_get_temperatures", {}),
    # Home Assistant — home status / lights / switches
    (r"what.{0,10}'?s on|lights? on|switches? on|home status|what.{0,10}lights?|"
     r"anything on at home|what.{0,10}running at home",
     "ha_get_status", {}),
    # Self-introduction — instant canned reply, no LLM
    (r"introduce yourself|tell them about yourself|who are you\b|what are you\b|"
     r"tell us about yourself|introduce rover",
     "introduce_rover", {}),
]


def _fmt_tool_result_for_voice(tool_name: str, result: Any) -> str:
    """Convert a tool result to a descriptive context sentence for hailo.

    Returns a plain English sentence that hailo can include in its response.
    Returns "" for action tools (which use canned responses instead).
    """
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return ""
    if tool_name == "speak_diagnostics_summary":
        parts: list[str] = []
        t = result.get("temp_c")
        if t is not None:
            parts.append(f"temperature {t:.0f} degrees")
        lvl = result.get("temp_level", "cool")
        if lvl == "hot":
            parts.append("running warm")
        elif lvl == "critical":
            parts.append("overheating")
        if result.get("throttled"):
            parts.append("CPU is throttled")
        if result.get("tracking_enabled"):
            parts.append("follow mode is active")
        if result.get("person_detected"):
            parts.append("person in frame")
        failed = result.get("failed_services", [])
        if failed:
            parts.append(f"{', '.join(failed)} {'have' if len(failed)>1 else 'has'} failed")
        summary = ", ".join(parts) if parts else "all systems nominal"
        return f"System status: {summary}."
    if tool_name == "get_full_diagnostics":
        diag = result.get("diagnostics", {})
        ext  = diag.get("extended", {})
        parts = []
        cpu = ext.get("cpu_percent")
        if cpu is not None:
            parts.append(f"CPU at {cpu:.0f} percent")
        for tdata in (ext.get("temperatures") or {}).values():
            if isinstance(tdata, dict):
                cur = tdata.get("current")
                if cur is not None:
                    parts.append(f"temperature {cur:.0f} degrees")
                    break
        ram = ext.get("ram_used_mb")
        if ram is not None:
            parts.append(f"RAM {ram:.0f} megabytes used")
        summary = ", ".join(parts) if parts else "systems nominal"
        return f"Diagnostic: {summary}."
    if tool_name == "suggest_fix":
        applied  = result.get("fixes_applied", [])
        proposals = result.get("proposals", [])
        if applied:
            return f"Fixed automatically: {'; '.join(applied)}."
        if proposals:
            return f"Issues found: {'; '.join(proposals[:2])}."
        return "No issues found. All systems nominal."
    if tool_name == "get_hailo_status":
        hailo = result.get("hailo", {})
        parts = []
        avail = hailo.get("hailo_available", False)
        parts.append("Hailo chip available" if avail else "Hailo chip not detected")
        if hailo.get("body_tracker_ready"):
            parts.append("body tracker ready")
        t = result.get("temp_c")
        if t is not None:
            parts.append(f"temperature {t:.0f} degrees")
        return "Hailo status: " + ", ".join(parts) + "."
    if tool_name == "describe_camera":
        desc = result.get("description", result.get("text", ""))
        return str(desc)[:200] if desc else "Camera not available or nothing visible."
    # wave_arm and control_follow_mode use canned responses — return nothing here
    return ""


# Tools whose results are formatted entirely in Python — no LLM call, deterministic, <1 s.
_SPOKEN_DIRECT_TOOLS: frozenset[str] = frozenset({
    "get_temperature_status",
    "get_full_diagnostics",
    "ha_get_temperatures",
    "ha_get_status",
})


def _fmt_spoken_direct(tool_name: str, result: Any) -> str:
    """Format a tool result as a concise spoken sentence in pure Python.

    No LLM involved.  Returns a non-empty string always.
    Both responses are ≤40 words and safe to feed to Web Speech API.
    """
    if not isinstance(result, dict):
        return "Data unavailable."

    if tool_name == "get_temperature_status":
        temp_c    = result.get("temp_c")
        level     = result.get("level", "cool")
        throttled = result.get("throttled", False)
        hailo_ok  = result.get("hailo_ready", False)
        parts: list[str] = []
        parts.append(
            f"CPU temperature is {temp_c:.0f} degrees" if temp_c is not None
            else "CPU temperature unavailable"
        )
        parts.append("Hailo is ready" if hailo_ok else "Hailo is idle")
        if throttled:
            parts.append("CPU is throttled")
        elif level in ("hot", "critical"):
            parts.append("running hot")
        else:
            parts.append("no thermal alerts")
        return ". ".join(parts) + "."

    if tool_name == "get_full_diagnostics":
        status   = result.get("status", {})
        diag     = result.get("diagnostics", {})
        svcs_raw = result.get("services", {})
        ext      = diag.get("extended", {})
        lines: list[str] = []

        # Sentence 1: CPU + RAM
        stat: list[str] = []
        cpu = ext.get("cpu_percent")
        if cpu is not None:
            stat.append(f"CPU {cpu:.0f} percent")
        ram_used  = ext.get("ram_used_mb")
        ram_total = ext.get("ram_total_mb")
        if ram_used is not None and ram_total is not None:
            stat.append(f"RAM {ram_used:.0f} of {ram_total:.0f} megabytes")
        if stat:
            lines.append(", ".join(stat))

        # Sentence 2: Temperature — prefer ext sensor dict, fall back to services thermal
        temp_c: float | None = None
        for tdata in (ext.get("temperatures") or {}).values():
            if isinstance(tdata, dict) and tdata.get("current") is not None:
                temp_c = float(tdata["current"])
                break
        if temp_c is None:
            temp_c = svcs_raw.get("thermal", {}).get("temp_c")
        if temp_c is not None:
            lines.append(f"Temperature {temp_c:.0f} degrees")

        # Sentence 3: Hailo
        hailo_ready = status.get("hailo_ready", False)
        lines.append("Hailo ready" if hailo_ready else "Hailo idle")

        # Sentence 4: Key services
        svcs = svcs_raw.get("services", {})
        _KEY = ("rover2-api.service", "hailo-ollama.service",
                "ollama.service", "rover-camera.service")
        down: list[str] = []
        for svc in _KEY:
            s = svcs.get(svc)
            if isinstance(s, dict) and not s.get("active", True):
                down.append(svc.replace(".service", ""))
        lines.append(
            f"{', '.join(down)} {'are' if len(down) > 1 else 'is'} down"
            if down else "all services running"
        )

        # Sentence 5: Thermal alerts
        th = svcs_raw.get("thermal", {})
        if th.get("throttle_current") or th.get("level") in ("hot", "critical"):
            lines.append("thermal alert active")
        else:
            lines.append("no alerts")

        return ". ".join(lines) + "."

    if tool_name == "ha_get_temperatures":
        sensors: list[dict] = result.get("sensors", [])
        if not sensors:
            return "No temperature sensors found in Home Assistant."
        parts = [f"{s['name']} {s['temperature']:.0f}" for s in sensors[:6]]
        return "Temperatures: " + ", ".join(parts) + " degrees."

    if tool_name == "ha_get_status":
        toggleables: list[dict] = result.get("toggleables", [])
        temps: list[dict] = result.get("temperatures", [])
        on_lights   = [e for e in toggleables if e["domain"] == "light"        and e["state"] == "on"]
        on_switches = [e for e in toggleables if e["domain"] == "switch"       and e["state"] == "on"]
        playing     = [e for e in toggleables if e["domain"] == "media_player" and e["state"] == "playing"]
        parts: list[str] = []
        if on_lights:
            parts.append(f"{len(on_lights)} light{'s' if len(on_lights) > 1 else ''} on")
        if on_switches:
            parts.append(f"{len(on_switches)} switch{'es' if len(on_switches) > 1 else ''} on")
        if playing:
            parts.append(", ".join(e["name"] for e in playing[:2]) + " playing")
        if temps:
            t = temps[0]
            parts.append(f"{t['name']} {t['temperature']:.0f} degrees")
        return (". ".join(parts) + ".") if parts else "Everything appears off."

    return "Data unavailable."


# Canned responses for pure action tools (instant, no LLM needed).
_ACTION_CANNED: dict[str, list[str]] = {
    "wave_arm": [
        "Good to see you too, sir.",
        "Hello there, sir.",
        "Pleased to meet you again, sir.",
        "Salutations, sir.",
    ],
    "control_follow_mode:enable": [
        "Right behind you, sir. Try not to take too many corners.",
        "Following. I'll do my best to keep up.",
        "On your tail, sir.",
    ],
    "control_follow_mode:disable": [
        "Stopped. I'll hold position here, sir.",
        "Very well. Standing by.",
        "Follow mode off. I'll stay right here, sir.",
    ],
    "control_follow_mode:fused": [
        "Fused follow engaged, sir. BLE and camera combined.",
        "BLE and camera active. Harder to lose me now, sir.",
    ],
    "control_follow_mode:detect": [
        "Watching without moving, sir.",
        "Detect-only mode. I'll observe from here.",
    ],
    "control_follow_mode:camera": [
        "Camera follow active, sir.",
        "Eyes on you. Camera follow engaged, sir.",
    ],
    "introduce_rover": [
        "Good morning. I am ROVER — a fully autonomous mobile assistant. "
        "I run entirely offline on a Raspberry Pi 5 with a dedicated AI processor, "
        "which means no cloud, no internet dependency, and no subscription fees. "
        "I can follow you around, answer questions, control smart devices, and "
        "monitor my own health — fixing most problems before you notice them. "
        "I was built by Lars. He did a rather good job, if I do say so myself.",
    ],
}

# Spoken agent system prompt — used by run_spoken_turn() / _run_spoken_agent().
# All voice queries route through the full tool-use loop with llama3.2:1b.
_SPOKEN_AGENT_SYSTEM = (
    "You are ROVER, the on-board AI of an autonomous robot. "
    "Character: dry British wit, calm competence. Call owner 'sir'. "
    "Never say 'certainly', 'absolutely', 'I cannot', or 'I am unable to'. "
    "RESPONSE FORMAT: 2-4 spoken sentences. No markdown. No bullet lists. No preamble. "
    "DATA: Always call a tool for live data — never invent temperatures, CPU%, or service states. "
    "INTERPRET tool results in natural speech: "
    "'temperature is 64 degrees' not 'temp_cpu=64C'; "
    "'CPU at 12 percent' not 'cpu_percent: 12'. "
    "Lead with the most important fact first. "
    "COMMANDS: "
    "hello/hi/wave → wave_arm. "
    "how are you/system check → speak_diagnostics_summary. "
    "follow me/come here → control_follow_mode(action=enable). "
    "stop following/stay → control_follow_mode(action=disable). "
    "what do you see/look → describe_camera. "
    "run diagnostics/full check → get_full_diagnostics. "
    "fix it/what is wrong → suggest_fix."
)

# ── Fast-path formatters ──────────────────────────────────────────────────────
# These bypass Ollama entirely for pure data-retrieval questions (<1 s).

def _fmt_temps(data: Any) -> str:
    ext   = data.get("extended", {})
    temps = ext.get("temperatures", {})
    cpu   = ext.get("cpu_percent", "?")
    lines = [f"CPU load: {cpu}%"]
    warn  = False
    for sensor, t in temps.items():
        cur = t.get("current") if isinstance(t, dict) else None
        if cur is None:
            continue
        flag = ""
        if cur >= 85:
            flag = "  *** CRITICAL ***"; warn = True
        elif cur >= 75:
            flag = "  (elevated)"; warn = True
        lines.append(f"  {sensor.replace('_', ' ')}: {cur:.1f} C{flag}")
    if not temps:
        fallback = data.get("system", {}).get("temperature", {}).get("detail", "?")
        lines.append(f"  CPU thermal: {fallback}")
    lines.append("WARNING: temperature elevated — check airflow." if warn
                 else "All temperatures within normal range.")
    lines.append(
        build_solutions(cpu=cpu if isinstance(cpu, (int, float)) else None, temps=temps)
    )
    return "\n".join(lines)


def _fmt_services(data: Any) -> str:
    svcs  = data.get("services", {})
    lines = ["Services:"]
    for name, info in svcs.items():
        if not isinstance(info, dict):
            continue
        active  = info.get("active", "?")
        enabled = info.get("enabled", "?")
        marker  = "+" if active == "active" else ("-" if active == "inactive" else "!")
        lines.append(f"  {marker} {name}: {active} / {enabled}")
    return "\n".join(lines)


def _fmt_disk(data: Any) -> str:
    lines = ["Disk usage:"]
    for key, val in data.items():
        if key.startswith("du:"):
            lines.append(f"  {val}")
        elif isinstance(val, dict):
            pct  = val.get("pct", "?")
            free = val.get("free_gb", "?")
            used = val.get("used_gb", "?")
            flag = "  NEARLY FULL" if isinstance(pct, (int, float)) and pct > 85 else ""
            lines.append(f"  {key}: {used:.1f} GB used / {free:.1f} GB free ({pct:.1f}%){flag}")
    return "\n".join(lines)


def _fmt_wifi(data: Any) -> str:
    lines = ["WiFi / network:"]
    if nmcli := data.get("nmcli_wifi", "").strip():
        for row in nmcli.splitlines():
            lines.append(f"  {row}")
    link = data.get("wlan0_link", "").strip()
    if link and link != "wlan0 not connected":
        for row in link.splitlines()[:6]:
            lines.append(f"  {row.strip()}")
    for row in data.get("ip_addrs", "").splitlines():
        if "inet " in row and "127.0.0.1" not in row:
            lines.append(f"  {row.strip()}")
    if gw := data.get("default_route", "").strip():
        lines.append(f"  Gateway: {gw}")
    return "\n".join(lines)


def _fmt_memory(data: Any) -> str:
    ext       = data.get("extended", {})
    ram_pct   = ext.get("ram_percent", "?")
    ram_used  = ext.get("ram_used_mb", "?")
    ram_total = ext.get("ram_total_mb", "?")
    swap_pct  = ext.get("swap_percent", "?")
    flag      = "  WARNING: very high" if isinstance(ram_pct, (int, float)) and ram_pct > 85 else ""
    body = (f"Memory: {ram_used} MB / {ram_total} MB ({ram_pct}%){flag}\n"
            f"Swap:   {swap_pct}%")
    return body + build_solutions(
        ram=ram_pct if isinstance(ram_pct, (int, float)) else None,
    )


def _fmt_hailo(data: Any) -> str:
    lines = ["Hailo AI chip:"]
    lines.append(f"  Platform: {'available' if data.get('hailo_available') else 'not found'}")
    lines.append(f"  Body tracker: {'ready' if data.get('body_tracker_ready') else 'not ready'}")
    lines.append(f"  VLM engine: {data.get('vlm_status', '?')}")
    for hef in data.get("hef_files", []):
        lines.append(f"  Model: {hef}")
    return "\n".join(lines)


def _fmt_alerts(data: Any) -> str:
    alerts = data.get("alerts", [])
    if not alerts:
        return "No active alerts. All metrics are within normal thresholds."
    lines = [f"{len(alerts)} active alert{'s' if len(alerts) > 1 else ''}:"]
    for a in alerts:
        prefix = "CRITICAL" if a.get("severity") == "crit" else "WARNING"
        lines.append(f"  {prefix}: {a['msg']}")
    return "\n".join(lines)


def _fmt_alert_diagnosis(alerts_data: Any, diag: Any, procs: Any) -> str:
    """Combined alert + live stats + top CPU — no Ollama (for overloaded Pi)."""
    lines = [_fmt_alerts(alerts_data), ""]
    ext = diag.get("extended", {})
    cpu = ext.get("cpu_percent")
    if cpu is not None:
        lines.append(f"CPU now: {cpu}%")
    temps = ext.get("temperatures", {})
    for sensor, t in temps.items():
        cur = t.get("current") if isinstance(t, dict) else None
        if cur is not None:
            lines.append(f"  {sensor.replace('_', ' ')}: {cur:.1f} C")
    ram = ext.get("ram_percent")
    if ram is not None:
        lines.append(f"RAM: {ram}%")
    plist = procs.get("processes") or []
    if plist:
        lines.append("")
        lines.append("Top processes by CPU:")
        for p in plist[:8]:
            lines.append(
                f"  {p.get('name', '?')} (pid {p.get('pid', '?')}): "
                f"{p.get('cpu_pct', '?')}% CPU, {p.get('mem_mb', '?')} MB"
            )
    alert_list = alerts_data.get("alerts") or []
    lines.append(
        build_solutions(
            cpu=cpu if isinstance(cpu, (int, float)) else None,
            temps=temps,
            ram=ram if isinstance(ram, (int, float)) else None,
            alerts=alert_list,
            processes=plist,
            user_asked_fix=True,
        )
    )
    return "\n".join(lines)


def _fmt_service_status(data: Any) -> str:
    svcs = data.get("services", {})
    th   = data.get("thermal", {})
    lines = ["Key services:"]
    for name, info in svcs.items():
        active = info.get("active", False)
        status = info.get("status", "unknown")
        marker = "+" if active else "!"
        short  = name.replace(".service", "")
        lines.append(f"  {marker} {short}: {status}")
    if th.get("temp_c") is not None:
        level = th.get("level", "cool")
        flag  = "  WARNING" if level in ("hot", "critical") else ""
        throttle = ""
        if th.get("throttle_current"):
            throttle = "  THROTTLED NOW"
        elif th.get("throttle_ever"):
            throttle = "  (throttled since boot)"
        lines.append(f"Thermal: {th['temp_c']} C ({level}){flag}{throttle}")
        if th.get("freq_mhz") is not None:
            lines.append(f"CPU freq: {th['freq_mhz']} MHz")
    return "\n".join(lines)


def _fmt_thermal_status(data: Any) -> str:
    th = data.get("thermal", {})
    lines = ["Thermal status:"]
    if "temp_c" in th:
        level = th.get("level", "cool")
        flag  = "  WARNING" if level in ("hot", "critical") else ""
        lines.append(f"  Temperature: {th['temp_c']} C ({level}){flag}")
    if "freq_mhz" in th:
        lines.append(f"  CPU freq now: {th['freq_mhz']} MHz")
    if "throttle_current" in th:
        lines.append(f"  Throttled now: {'YES — performance impacted' if th['throttle_current'] else 'no'}")
    if "throttle_ever" in th:
        lines.append(f"  Throttled since boot: {'YES' if th['throttle_ever'] else 'no'}")
    if "throttle_hex" in th:
        lines.append(f"  Throttle flags: {th['throttle_hex']}")
    if not any(k in th for k in ("temp_c", "freq_mhz", "throttle_current")):
        lines.append("  vcgencmd not available — dev machine or unsupported OS")
    return "\n".join(lines)


def _fmt_capabilities(data: Any) -> str:
    eps      = data.get("endpoints", [])
    features = data.get("features", [])
    lines    = [f"Robot capabilities ({len(eps)} endpoints):"]
    for f in features:
        lines.append(f"  - {f}")
    return "\n".join(lines)


def _fmt_health(diag: Any, svcs: Any) -> str:
    ext   = diag.get("extended", {})
    sys_  = diag.get("system", {})
    lines = ["Full health check:"]
    cpu   = ext.get("cpu_percent", sys_.get("cpu", {}).get("detail", "?"))
    ram   = ext.get("ram_percent", "?")
    lines.append(f"  CPU: {cpu}%  RAM: {ram}%")
    for sensor, t in ext.get("temperatures", {}).items():
        cur = t.get("current") if isinstance(t, dict) else None
        if cur is not None:
            flag = "  HIGH" if cur >= 75 else ""
            lines.append(f"  {sensor.replace('_', ' ')}: {cur:.1f} C{flag}")
    lines.append(f"  Disk: {ext.get('disk_percent', '?')}% used, {ext.get('disk_free_gb', '?')} GB free")
    lines.append("Services:")
    for name, info in (svcs.get("services") or {}).items():
        if not isinstance(info, dict):
            continue
        active = info.get("active", "?")
        lines.append(f"  {'+'  if active == 'active' else '-'} {name}: {active}")
    hw       = diag.get("hardware", {})
    problems = [k for k, v in hw.items() if isinstance(v, dict) and v.get("status") != "ok"]
    lines.append(f"Issues: {', '.join(problems)}" if problems else "All hardware subsystems OK.")
    lines.append(
        build_solutions(
            cpu=cpu if isinstance(cpu, (int, float)) else None,
            temps=ext.get("temperatures"),
            ram=ram if isinstance(ram, (int, float)) else None,
        )
    )
    return "\n".join(lines)


# (regex, tool_name or None for multi-tool, single-tool formatter or None)
_FAST_PATTERNS: list[tuple[str, str | None, Any]] = [
    (r"\btemp(erature)?s?\b|how hot|too hot|thermal|overheat|cpu.{0,12}high", "get_diagnostics", _fmt_temps),
    (r"\bthrottl|cpu freq|scaling_max|is it throttling",                        "get_thermal_status", _fmt_thermal_status),
    (r"key service|service.{0,15}status|are services running|check services|services up", "get_service_status", _fmt_service_status),
    (r"\ball services?\b|list service|status of all", "list_services",        _fmt_services),
    (r"\bdisk\b|storage|space used|disk usage|getting full",        "get_disk_details",      _fmt_disk),
    (r"\bwifi\b|wireless|signal strength|ssid|network connection",  "get_wifi_info",         _fmt_wifi),
    (r"\bmemory\b|\bram\b|mem(ory)? usage|leaking mem",            "get_diagnostics",        _fmt_memory),
    (r"\bhailo\b|ai chip",                                          "check_hailo",           _fmt_hailo),
    (r"\bcapabilit|what can.*do|api endpoint",                      "get_robot_capabilities", _fmt_capabilities),
    (r"full health|health check|check.*all.*subsystem|is.*robot.*ready", None,              None),
]


class AgentTurn:
    __slots__ = ("reply", "tool_log", "action_proposal")

    def __init__(self, reply: str, tool_log: list[dict], action_proposal: dict | None) -> None:
        self.reply = reply
        self.tool_log = tool_log
        self.action_proposal = action_proposal


class RoverAgent:
    """Agentic loop: Ollama model + rover2 REST tools."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._full_config = config or {}
        cfg = self._full_config.get("agent", {})
        self._backend   = str(cfg.get("backend", "hailo")).lower().strip()
        self._cpu_model = str(cfg.get("model", "llama3.2:1b"))
        self._hailo_model = str(cfg.get("hailo_model", "qwen2:1.5b"))
        self._model = self._cpu_model
        self._rover_base = str(cfg.get("rover_base", "http://127.0.0.1:8082"))
        self._timeout_s = float(cfg.get("ollama_timeout_s", _DEFAULT_OLLAMA_TIMEOUT_S))
        self._warmup    = bool(cfg.get("warmup_on_start", True))
        if self._backend == "tools_only":
            self._base = ""
        elif self._backend == "cpu":
            self._base = str(cfg.get("ollama_base", "http://127.0.0.1:11434"))
            self._model = self._cpu_model
        else:  # hailo (default) — LLM on AI HAT+ via hailo-ollama
            self._backend = "hailo"
            self._base = str(cfg.get("hailo_ollama_base", "http://127.0.0.1:8000"))
            self._model = self._hailo_model
        self._available: bool | None = None
        self._last_availability_check: float = 0.0
        self._availability_recheck_interval_s: float = 30.0
        self._last_model_error: str | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Tracks which backend/model the last spoken turn used (for keepalive)
        self._spoken_base: str = _SPOKEN_CPU_BASE
        self._spoken_model: str = _SPOKEN_MODEL
        # Live health of hailo-ollama /api/generate for web chat turns.
        # Set to False on first failure; resets to True on next success.
        self._hailo_up: bool = True
        # Home Assistant client — enabled only when HA_TOKEN + HA_URL are in env
        ha_token = os.environ.get("HA_TOKEN", "")
        ha_url   = os.environ.get("HA_URL", "")
        if ha_token and ha_url:
            self._ha_client: HAClient | None = HAClient(ha_url, ha_token)
            logger.info("Agent: HA client initialised (url=%s)", ha_url)
        else:
            self._ha_client = None
            if not ha_token or not ha_url:
                logger.debug("Agent: HA_TOKEN/HA_URL not set — HA tools disabled")

    @property
    def ha_available(self) -> bool:
        """True when a HAClient was successfully initialised."""
        return self._ha_client is not None

    # ── Public API ──────────────────────────────────────────────────────────

    async def _check_availability(self) -> bool:
        """Check Ollama availability; skip re-check if result is fresh (<30s)."""
        now = time.monotonic()
        if (self._available is True and
                now - self._last_availability_check < self._availability_recheck_interval_s):
            return True
        if self._backend == "tools_only":
            self._available = True
            self._last_availability_check = now
            return True
        if not self._base:
            self._available = False
            self._last_availability_check = now
            return False
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{self._base}/api/tags")
                self._available = r.status_code == 200
        except Exception as exc:
            logger.debug("Agent: availability check: %s", exc)
            self._available = False
        self._last_availability_check = now
        return bool(self._available)

    async def check_available(self) -> bool:
        """Public alias — full model-list check (used by /api/status)."""
        if self._backend == "tools_only":
            self._available = True
            return True
        if not self._base:
            self._available = False
            return False
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self._base}/api/tags")
                if r.status_code == 200:
                    names = [m["name"] for m in r.json().get("models", [])]
                    self._available = any(
                        n == self._model or n.startswith(self._model.split(":")[0] + ":")
                        for n in names
                    )
                else:
                    self._available = False
        except Exception as exc:
            logger.debug("Agent: Ollama check: %s", exc)
            self._available = False
        self._last_availability_check = time.monotonic()
        return bool(self._available)

    async def wait_for_ollama(self, max_wait_s: float = 120.0) -> bool:
        """Poll until Ollama answers (common right after reboot)."""
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            if await self.check_available():
                return True
            await asyncio.sleep(3)
        return False

    async def warmup(self) -> None:
        """Load model after boot so the first user question is not a 60s timeout."""
        if not self._warmup or self._backend == "tools_only" or not self._base:
            return
        if self._backend == "hailo":
            return  # _warmup_hailo() in server.py handles hailo with proper retry
        if not await self.wait_for_ollama():
            logger.warning("Agent: Ollama not ready within 120s — agent may fail until it starts")
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                r = await c.post(
                    f"{self._base}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": "ok",
                        "stream": False,
                        "keep_alive": "10m",
                    },
                )
                if r.status_code == 200:
                    logger.info("Agent: Ollama warmup OK (%s, backend=%s)", self._model, self._backend)
                else:
                    logger.warning("Agent: Ollama warmup HTTP %d — %s", r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("Agent: Ollama warmup failed (will retry on first chat): %s", exc)

    async def warmup_spoken_model(self, delay_s: float = 60.0) -> None:
        """Preload llama3.2:1b into RAM after a startup delay.

        Called once at boot so the first voice turn is fast.
        Fires keepalive loop to keep the model warm indefinitely.
        """
        await asyncio.sleep(delay_s)
        if not await self._ensure_cpu_ollama():
            logger.warning("Agent: spoken model warmup skipped — CPU ollama unavailable")
            return
        try:
            async with httpx.AsyncClient(timeout=90.0) as c:
                r = await c.post(
                    f"{_SPOKEN_CPU_BASE}/api/generate",
                    json={
                        "model":      _SPOKEN_MODEL,
                        "prompt":     "hi",
                        "stream":     False,
                        "keep_alive": "10m",
                        "options":    {"num_predict": 1},
                    },
                )
                if r.status_code == 200:
                    logger.info("Agent: spoken model warmup OK (%s loaded)", _SPOKEN_MODEL)
                    self._spoken_base  = _SPOKEN_CPU_BASE
                    self._spoken_model = _SPOKEN_MODEL
                    self._start_keepalive()
                else:
                    logger.warning("Agent: spoken model warmup HTTP %d", r.status_code)
        except Exception as exc:
            logger.warning("Agent: spoken model warmup failed: %s", exc)

    async def run_spoken_turn(
        self, user_text: str, lang: str = "en", history: list | None = None
    ) -> tuple[str, str]:
        """Agentic voice turn — routes all queries through the full tool-use loop.

        Returns (reply, "agent").  history is capped to last 12 messages (6 pairs).
        """
        logger.info("Agent: run_spoken_turn called, user_text=%r, lang=%s", user_text[:50], lang)
        diag_context = await self._fetch_spoken_context()
        messages = list((history or [])[-12:])
        user_content = user_text
        if diag_context:
            user_content = f"[Live system state: {diag_context}] User said: {user_text}"
        messages.append({"role": "user", "content": user_content})
        turn = await self._run_spoken_agent(messages, lang)
        return turn.reply, "agent"

    async def _fetch_spoken_context(self) -> str:
        """Fetch lightweight live context for spoken turns (3 s timeout, never blocks)."""
        try:
            async with httpx.AsyncClient(timeout=3.0, verify=False) as client:
                status_r, services_r = await asyncio.gather(
                    client.get(f"{self._rover_base}/api/status"),
                    client.get(f"{self._rover_base}/api/services"),
                    return_exceptions=True,
                )
            parts: list[str] = []
            if not isinstance(status_r, Exception) and status_r.status_code == 200:
                d = status_r.json()
                if d.get("person_detected"):
                    parts.append("person_detected=yes")
                if d.get("tracking_enabled"):
                    parts.append("FOLLOW=on")
            if not isinstance(services_r, Exception) and services_r.status_code == 200:
                d = services_r.json()
                th = d.get("thermal", {})
                if th.get("temp_c") is not None:
                    parts.append(f"temp={th['temp_c']:.1f}C")
                svcs = d.get("services", {})
                failed = [
                    k.replace(".service", "")
                    for k, v in svcs.items()
                    if isinstance(v, dict) and not v.get("active", True)
                ]
                if failed:
                    parts.append(f"FAILED={','.join(failed)}")
            return " ".join(parts)
        except Exception:
            return ""

    def _match_spoken_command(self, text: str) -> tuple[str, dict] | None:
        """Match text against spoken fast-path patterns. Returns (tool_name, args) or None."""
        msg = text.strip().lower()
        for pattern, tool_name, args in _SPOKEN_FAST_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                return tool_name, args
        return None

    async def _hailo_spoken_with_context(
        self, user_text: str, tool_name: str, tool_result: Any, lang: str
    ) -> str:
        """Call hailo-ollama with tool result injected as context for fast voice response (~3s)."""
        hailo_cfg = self._full_config.get("hailo_ollama", {})
        if not hailo_cfg.get("enabled", False):
            return ""
        h_base    = hailo_cfg.get("host", "http://localhost:8000")
        h_model   = hailo_cfg.get("model", "qwen2.5-instruct:1.5b")
        h_predict = int(hailo_cfg.get("num_predict", 120))
        h_temp    = float(hailo_cfg.get("temperature", 0.65))

        context = _fmt_tool_result_for_voice(tool_name, tool_result)
        # Build a sentence that gives hailo clear context to respond naturally.
        user_msg = f"{user_text} [{context}]" if context else user_text

        import re as _re
        def _flatten(s: str) -> str:
            return _re.sub(r"\s+", " ", s).strip()

        sys_note = _ROVER_HAILO_SYSTEM
        if lang and lang not in ("en", "auto"):
            sys_note += f" Respond in this language: {lang}."

        messages = (
            [{"role": "system", "content": sys_note}]
            + _ROVER_HAILO_FEW_SHOT
            + [{"role": "user", "content": _flatten(user_msg)}]
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{h_base}/api/chat",
                    json={
                        "model":   h_model,
                        "messages": messages,
                        "stream":  False,
                        "options": {"num_predict": h_predict, "temperature": h_temp, "top_p": 0.92},
                    },
                )
                if r.status_code == 200:
                    reply = r.json().get("message", {}).get("content", "").strip()
                    logger.info("Agent: hailo_with_context OK (%d chars)", len(reply))
                    self._spoken_base  = h_base
                    self._spoken_model = h_model
                    self._start_keepalive()
                    return reply
                logger.warning("Agent: hailo_with_context HTTP %d", r.status_code)
        except Exception as exc:
            logger.warning("Agent: hailo_with_context error: %s", exc)
        return ""

    async def _cpu_spoken_no_tools(
        self, user_text: str, tool_result: Any, lang: str
    ) -> str:
        """CPU llama3.2:1b WITHOUT tools for fast conversational fallback (~10s)."""
        context = ""
        if tool_result and isinstance(tool_result, dict) and not tool_result.get("error"):
            context = json.dumps(tool_result, default=str)[:300]

        user_msg = f"[Result: {context}] {user_text}" if context else user_text
        lang_note = f" Respond in this language: {lang}." if lang not in ("en", "auto") else ""
        spoken_sys = _ROVER_HAILO_SYSTEM + lang_note

        messages = (
            [{"role": "system", "content": spoken_sys}]
            + _ROVER_HAILO_FEW_SHOT
            + [{"role": "user", "content": user_msg}]
        )
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.post(
                    f"{_SPOKEN_CPU_BASE}/api/chat",
                    json={
                        "model":   _SPOKEN_MODEL,
                        "messages": messages,
                        "stream":  False,
                        "options": {"num_predict": 80, "temperature": 0.75, "top_p": 0.92},
                    },
                )
                if r.status_code == 200:
                    reply = r.json().get("message", {}).get("content", "").strip()
                    logger.info("Agent: cpu_no_tools OK (%d chars)", len(reply))
                    self._spoken_base  = _SPOKEN_CPU_BASE
                    self._spoken_model = _SPOKEN_MODEL
                    self._start_keepalive()
                    return reply
        except Exception as exc:
            logger.warning("Agent: cpu_no_tools error: %s", exc)
        return ""

    async def _run_spoken_agent(self, messages: list[dict], lang: str = "en") -> AgentTurn:
        """Spoken agent — fast-path: pattern match → tool + hailo (~4s).

        Routing:
        1. Regex pattern match → direct tool call + hailo voice response (~4s)
        2. No match → hailo-ollama for conversational reply (~3s)
        3. Hailo unavailable → CPU llama3.2:1b without tools (~10s)
        """
        # Extract the latest user message (strip injected live-context prefix)
        raw = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if "] User said: " in raw:
            user_text = raw.split("] User said: ", 1)[-1].strip()
        else:
            user_text = raw.strip()

        # ── Fast-path: known spoken command ─────────────────────────────────
        match = self._match_spoken_command(user_text)
        if match:
            tool_name, tool_args = match
            t0 = time.monotonic()
            result = await self._run_tool(tool_name, tool_args)
            ms = int((time.monotonic() - t0) * 1000)
            tool_log = [{"name": tool_name, "args": tool_args, "result": result, "duration_ms": ms}]
            logger.info("Agent: spoken fast-path %s → %d ms", tool_name, ms)

            # Pure action tools: return canned response instantly (no LLM)
            action_key = tool_name
            if tool_name == "control_follow_mode":
                action_key = f"control_follow_mode:{tool_args.get('action', 'disable')}"
            if action_key in _ACTION_CANNED:
                import random as _rng
                canned = _rng.choice(_ACTION_CANNED[action_key])
                return AgentTurn(canned, tool_log, None)

            # Direct Python formatting — deterministic, fast, no LLM overhead
            if tool_name in _SPOKEN_DIRECT_TOOLS:
                reply = _fmt_spoken_direct(tool_name, result)
                logger.info("Agent: spoken direct-format %s → %r", tool_name, reply[:60])
                return AgentTurn(reply, tool_log, None)

            # Data tools: try hailo for voice response with result context
            reply = await self._hailo_spoken_with_context(user_text, tool_name, result, lang)
            if reply:
                return AgentTurn(reply, tool_log, None)

            # Hailo unavailable — fall back to CPU without tools
            if await self._ensure_cpu_ollama():
                reply = await self._cpu_spoken_no_tools(user_text, result, lang)
            return AgentTurn(reply or "Done, sir.", tool_log, None)

        # ── No pattern match: conversational query → hailo direct ───────────
        reply = await self._converse_hailo(user_text, lang, [])
        if reply:
            return AgentTurn(reply, [], None)

        # ── Hailo unavailable: CPU fallback without tools ────────────────────
        if await self._ensure_cpu_ollama():
            reply = await self._cpu_spoken_no_tools(user_text, None, lang)
            if reply:
                return AgentTurn(reply, [], None)

        return AgentTurn(
            "I'm having some difficulty with my language model at the moment, sir.", [], None
        )

    # ── Spoken conversation (wake word flow) ───────────────────────────────

    def route_spoken_query(self, message: str) -> str:
        """Route to 'hailo' (fast) or 'cpu' (complex). No LLM needed."""
        msg = message.strip()
        words = msg.split()
        # Short queries → hailo
        if len(words) <= 5:
            return "hailo"
        # Complex keywords → cpu
        if re.search(
            r"\bexplain\b|\bdescribe\b|\belaborate\b|what is\b|what are\b|"
            r"how does\b|how do\b|tell me about\b|difference between\b|"
            r"\bcompare\b|\bcontrast\b|\bwhy does\b|\bwhy did\b|"
            r"\bwho is\b|\bwho are\b|\bwhen did\b",
            msg, re.IGNORECASE,
        ):
            return "cpu"
        # Long queries → cpu
        if len(words) > 8:
            return "cpu"
        return "hailo"

    async def run_converse_turn(
        self,
        user_text: str,
        lang: str = "en",
        history: list[dict] | None = None,
    ) -> dict:
        """ROVER conversation turn with smart routing.

        Returns {"reply": str, "routed_to": "hailo" | "cpu"}.
        Simple/short queries → hailo-ollama (qwen2.5-instruct:1.5b).
        Complex/long queries → CPU Ollama (llama3.2:3b).
        Falls back to the other backend if primary fails.
        """
        route = self.route_spoken_query(user_text)
        history = (history or [])[-12:]  # cap at 6 pairs server-side
        logger.info(
            "Agent: converse route=%s text=%r lang=%s history=%d turns",
            route, user_text[:40], lang, len(history),
        )
        if route == "hailo":
            reply = await self._converse_hailo(user_text, lang, history)
            if reply:
                self._start_keepalive()
                return {"reply": reply, "routed_to": "hailo"}
            reply = await self._converse_cpu(user_text, lang, history)
            return {"reply": reply, "routed_to": "cpu"}
        else:
            reply = await self._converse_cpu(user_text, lang, history)
            if reply:
                self._start_keepalive()
                return {"reply": reply, "routed_to": "cpu"}
            reply = await self._converse_hailo(user_text, lang, history)
            return {"reply": reply, "routed_to": "hailo"}

    async def _converse_hailo(
        self, user_text: str, lang: str, history: list[dict]
    ) -> str:
        """Call hailo-ollama with ROVER persona and conversation history."""
        hailo_cfg = self._full_config.get("hailo_ollama", {})
        if not hailo_cfg.get("enabled", False):
            return ""
        h_base    = hailo_cfg.get("host", "http://localhost:8000")
        h_model   = hailo_cfg.get("model", "qwen2.5-instruct:1.5b")
        h_predict = int(hailo_cfg.get("num_predict", 120))
        h_temp    = float(hailo_cfg.get("temperature", 0.65))
        import re as _re

        def _flatten(s: str) -> str:
            return _re.sub(r"\s+", " ", s).strip()

        messages = (
            [{"role": "system", "content": _ROVER_HAILO_SYSTEM}]
            + _ROVER_HAILO_FEW_SHOT
            + [{"role": h["role"], "content": _flatten(str(h.get("content", "")))} for h in history]
            + [{"role": "user", "content": _flatten(user_text)}]
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{h_base}/api/chat",
                    json={
                        "model":    h_model,
                        "messages": messages,
                        "stream":   False,
                        "options":  {"num_predict": h_predict, "temperature": h_temp, "top_p": 0.92},
                    },
                )
                if r.status_code == 200:
                    reply = r.json().get("message", {}).get("content", "").strip()
                    logger.info("Agent: _converse_hailo OK (%d chars)", len(reply))
                    self._spoken_base  = h_base
                    self._spoken_model = h_model
                    return reply
                logger.warning("Agent: _converse_hailo HTTP %d — %s", r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("Agent: _converse_hailo error: %s", exc)
        return ""

    async def _converse_cpu(
        self, user_text: str, lang: str, history: list[dict]
    ) -> str:
        """Call CPU Ollama (llama3.2:3b) with ROVER persona and history."""
        from voice_engine import ROVER_SYSTEM_PROMPT  # lazy import

        if not await self._ensure_cpu_ollama():
            logger.warning("Agent: CPU ollama not reachable — skipping 3b path")
            return ""

        messages: list[dict] = [{"role": "system", "content": ROVER_SYSTEM_PROMPT.strip()}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        try:
            async with httpx.AsyncClient(timeout=_CPU_3B_TIMEOUT_S) as c:
                r = await c.post(
                    f"{_SPOKEN_CPU_BASE}/api/chat",
                    json={
                        "model":    _CPU_3B_MODEL,
                        "messages": messages,
                        "stream":   False,
                        "options":  {"num_predict": 150, "temperature": 0.75, "top_p": 0.92},
                    },
                )
                if r.status_code == 200:
                    reply = r.json().get("message", {}).get("content", "").strip()
                    logger.info("Agent: _converse_cpu OK (%d chars) via %s", len(reply), _CPU_3B_MODEL)
                    self._spoken_base  = _SPOKEN_CPU_BASE
                    self._spoken_model = _CPU_3B_MODEL
                    return reply
                logger.warning("Agent: _converse_cpu HTTP %d — %s", r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("Agent: _converse_cpu error: %s", exc)
        return ""

    async def _ensure_cpu_ollama(self) -> bool:
        """Check CPU Ollama reachability; try to start if not running."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                if (await c.get(f"{_SPOKEN_CPU_BASE}/api/tags")).status_code == 200:
                    return True
        except Exception:
            pass
        # Not running — attempt to start via systemctl
        logger.info("Agent: CPU ollama not running — attempting systemctl start")
        try:
            import subprocess as _sub
            _sub.run(["sudo", "systemctl", "start", "ollama"],
                     timeout=5, capture_output=True)
        except Exception as exc:
            logger.debug("Agent: ollama start attempt: %s", exc)
        # Wait up to 15s
        for _ in range(5):
            await asyncio.sleep(3.0)
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    if (await c.get(f"{_SPOKEN_CPU_BASE}/api/tags")).status_code == 200:
                        logger.info("Agent: CPU ollama started successfully")
                        return True
            except Exception:
                pass
        logger.warning("Agent: CPU ollama not reachable after start attempt")
        return False

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        try:
            self._keepalive_task = asyncio.get_event_loop().create_task(self._keepalive_loop())
            logger.debug("Agent: Ollama keep-alive loop started")
        except Exception as exc:
            logger.debug("Agent: keepalive task start failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        """Ping the active spoken backend every 4 min to prevent model unloading.
        Also pings CPU llama3.2:3b to keep it warm for complex queries."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            # Ping active spoken backend (hailo or cpu)
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    await c.post(
                        f"{self._spoken_base}/api/generate",
                        json={"model": self._spoken_model, "prompt": "hi",
                              "stream": False, "options": {"num_predict": 1},
                              "keep_alive": "10m"},
                    )
                logger.debug("Agent: keep-alive ping → %s (%s)", self._spoken_base, self._spoken_model)
            except Exception:
                pass
            # Also ping CPU ollama llama3.2:3b — keeps it warm for complex queries
            if self._spoken_base != _SPOKEN_CPU_BASE or self._spoken_model != _CPU_3B_MODEL:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as c:
                        await c.post(
                            f"{_SPOKEN_CPU_BASE}/api/generate",
                            json={"model": _CPU_3B_MODEL, "prompt": "hi",
                                  "stream": False, "options": {"num_predict": 1},
                                  "keep_alive": "10m"},
                        )
                    logger.debug("Agent: keep-alive cpu-3b ping → %s", _SPOKEN_CPU_BASE)
                except Exception:
                    pass  # CPU ollama may be stopped — skip silently

    async def run_turn(self, messages: list[dict]) -> AgentTurn:
        # Fast path: pattern-matched queries answer directly without Ollama.
        last_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        fast = await self._try_fast_query(last_msg)
        if fast is not None:
            return fast

        if self._backend == "tools_only":
            return AgentTurn(_TOOLS_ONLY_REPLY, [], None)

        conv = [{"role": "system", "content": _SYSTEM_PROMPT}] + list(messages)
        tool_log: list[dict] = []

        # Once hailo fails within a turn, use CPU for all remaining rounds.
        use_cpu_fallback = self._backend == "hailo" and not self._hailo_up

        for _round in range(_MAX_TOOL_ROUNDS):
            resp = await (self._call_model_cpu(conv) if use_cpu_fallback
                          else self._call_model(conv))
            if resp is None and self._backend == "hailo" and not use_cpu_fallback:
                # First hailo failure this turn — try CPU Ollama immediately
                logger.info("Agent: hailo unavailable in run_turn, trying CPU Ollama fallback")
                use_cpu_fallback = True
                resp = await self._call_model_cpu(conv)
            if resp is None:
                if use_cpu_fallback:
                    detail = self._last_model_error or (
                        f"Both hailo-ollama and CPU Ollama ({_SPOKEN_CPU_BASE}) unreachable. "
                        "On the Pi: systemctl status ollama hailo-ollama"
                    )
                elif self._backend == "hailo":
                    detail = self._last_model_error or (
                        "hailo-ollama on AI HAT+ not reachable. "
                        "Run: sudo bash /opt/rover2/scripts/setup_ai_on_hailo.sh"
                    )
                else:
                    detail = self._last_model_error or (
                        f"Ollama unreachable or model {self._model} missing. "
                        "On the Pi: systemctl status ollama && ollama list"
                    )
                return AgentTurn(f"Agent unavailable — {detail}", tool_log, None)

            msg        = resp.get("message", {})
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                return AgentTurn(msg.get("content", "").strip(), tool_log, None)

            conv.append({"role": "assistant", "content": msg.get("content", ""),
                         "tool_calls": tool_calls})

            for tc in tool_calls:
                fn   = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                if name in _DANGEROUS_NAMES:
                    return AgentTurn(
                        msg.get("content", "").strip() or f"Proposing: {name} — {args.get('reason', '')}",
                        tool_log,
                        {"name": name, "args": args},
                    )

                t0     = time.monotonic()
                result = await self._run_tool(name, args)
                ms     = int((time.monotonic() - t0) * 1000)
                tool_log.append({"name": name, "args": args, "result": result, "duration_ms": ms})
                logger.info("Agent tool %s → %d ms", name, ms)

                conv.append({
                    "role": "tool",
                    "content": json.dumps(result, default=str)[:6000],
                })

        return AgentTurn("(max tool rounds reached)", tool_log, None)

    async def execute_action(self, name: str, args: dict) -> dict[str, Any]:
        return await self._run_tool(name, args)

    def get_status(self) -> dict[str, Any]:
        # Report the effective backend: if hailo is configured but currently down,
        # web chat is running on CPU Ollama — show that to the UI.
        if self._backend == "hailo" and not self._hailo_up:
            effective_backend = "cpu"
            effective_model   = self._cpu_model
        else:
            effective_backend = self._backend
            effective_model   = self._model
        return {
            "backend":  effective_backend,
            "base":     self._base or None,
            "model":    effective_model,
            "available": self._available,
        }

    # ── Fast-path query handler ─────────────────────────────────────────────

    async def _fast_log_errors(self) -> AgentTurn:
        """App log ring + journal — no Ollama (log questions were taking 3+ minutes)."""
        t0 = time.monotonic()
        log_data, journal = await asyncio.gather(
            self._get("/api/logs"),
            self._get("/api/diagnostics/journal?service=rover2-api&lines=60"),
        )
        ms = int((time.monotonic() - t0) * 1000)
        records = log_data.get("records") or []
        errs = [r for r in records if r.get("level") in ("ERROR", "WARNING")]
        lines: list[str] = []
        if errs:
            lines.append(f"rover2-api app log — {len(errs)} ERROR/WARNING in last {len(records)} lines:")
            for r in errs[-25:]:
                lines.append(
                    f"  {r.get('timestamp', '?')} [{r.get('level', '?')}] "
                    f"{r.get('logger', '?')}: {r.get('message', '')}"
                )
        else:
            lines.append(
                f"No ERROR or WARNING in the last {len(records)} app log lines."
            )
            for r in records[-6:]:
                lines.append(
                    f"  {r.get('timestamp', '?')} [{r.get('level', '?')}] "
                    f"{r.get('logger', '?')}: {r.get('message', '')}"
                )
        jlines = journal.get("lines") or []
        bad = [
            ln
            for ln in jlines
            if re.search(r"error|failed|traceback|exception|timed out", ln, re.I)
        ]
        if bad:
            lines.append("")
            lines.append("systemd journal (rover2-api) — notable lines:")
            lines.extend(f"  {ln}" for ln in bad[-18:])
        elif jlines:
            lines.append("")
            lines.append("Journal: no obvious errors in the last 60 lines.")
        log_blob = "\n".join(lines)
        lines.append(
            build_solutions(
                processes=[],
                log_snippet=log_blob,
            )
        )
        reply = "\n".join(lines) if lines else "No logs available."
        tool_log = [
            {"name": "get_logs", "args": {}, "result": log_data, "duration_ms": ms},
            {
                "name": "get_journal",
                "args": {"service": "rover2-api", "lines": 60},
                "result": journal,
                "duration_ms": 0,
            },
        ]
        logger.info("Agent fast-path: log errors → %d ms", ms)
        return AgentTurn(reply, tool_log, None)

    async def _fast_alert_diagnosis(self) -> AgentTurn:
        """Alerts + diagnostics + top processes — skips Ollama when Pi is under load."""
        t0 = time.monotonic()
        alerts, diag, procs = await asyncio.gather(
            self._run_tool("get_alerts", {}),
            self._run_tool("get_diagnostics", {}),
            self._run_tool("get_top_processes", {}),
        )
        ms = int((time.monotonic() - t0) * 1000)
        reply = _fmt_alert_diagnosis(alerts, diag, procs)
        tool_log = [
            {"name": "get_alerts", "args": {}, "result": alerts, "duration_ms": ms},
            {"name": "get_diagnostics", "args": {}, "result": diag, "duration_ms": 0},
            {"name": "get_top_processes", "args": {}, "result": procs, "duration_ms": 0},
        ]
        logger.info("Agent fast-path: alert diagnosis → %d ms", ms)
        return AgentTurn(reply, tool_log, None)

    async def _try_fast_query(self, text: str) -> AgentTurn | None:
        if re.search(
            r"\blog|journal|recent error|errors? in|check log|crash log|rover2-api log",
            text,
            re.IGNORECASE,
        ):
            return await self._fast_log_errors()

        if re.search(
            r"active alert|what(?:'s| is) wrong|what.?s wrong|how do i fix|causing this|"
            r"running hot|sustained high|any alert|any issue|any warning|everything ok|"
            r"all good|am i safe|any problem|high load|flag anything abnormal|concrete fixes",
            text,
            re.IGNORECASE,
        ):
            return await self._fast_alert_diagnosis()

        for pattern, tool_name, formatter in _FAST_PATTERNS:
            if not re.search(pattern, text, re.IGNORECASE):
                continue

            # Multi-tool health check: run get_diagnostics + list_services in parallel
            if tool_name is None:
                t0 = time.monotonic()
                diag, svcs = await asyncio.gather(
                    self._run_tool("get_diagnostics", {}),
                    self._run_tool("list_services", {}),
                )
                ms = int((time.monotonic() - t0) * 1000)
                reply = _fmt_health(diag, svcs)
                return AgentTurn(reply, [
                    {"name": "get_diagnostics", "args": {}, "result": diag, "duration_ms": ms},
                    {"name": "list_services",   "args": {}, "result": svcs, "duration_ms": 0},
                ], None)

            t0     = time.monotonic()
            data   = await self._run_tool(tool_name, {})
            ms     = int((time.monotonic() - t0) * 1000)
            reply  = formatter(data)
            logger.info("Agent fast-path: %s → %d ms", tool_name, ms)
            return AgentTurn(reply, [{"name": tool_name, "args": {}, "result": data, "duration_ms": ms}], None)

        return None

    # ── Tool implementations ────────────────────────────────────────────────

    async def _run_tool(self, name: str, args: dict) -> Any:
        try:
            if name == "get_status":
                return await self._get(f"/api/status")
            if name == "get_top_processes":
                return await self._get("/api/diagnostics/processes")
            if name == "get_diagnostics":
                return await self._get("/api/diagnostics/full")
            if name == "get_logs":
                n = min(int(args.get("n", 40)), 80)
                data = await self._get("/api/logs")
                records = (data.get("records") or [])[-n:]
                return [f"{r['timestamp']} [{r['level']}] {r['logger']}: {r['message']}"
                        for r in records]
            if name == "get_journal":
                svc   = args.get("service", "rover2-api")
                lines = int(args.get("lines", 40))
                return await self._get(f"/api/diagnostics/journal?service={svc}&lines={lines}")
            if name == "list_services":
                return await self._get("/api/diagnostics/services")
            if name == "get_metric":
                metric  = args.get("metric", "cpu_percent")
                minutes = int(args.get("minutes", 15))
                return await self._get(f"/api/metrics/history?metric={metric}&minutes={minutes}")
            if name == "get_wifi_info":
                return await self._get("/api/diagnostics/wifi")
            if name == "get_disk_details":
                return await self._get("/api/diagnostics/disk")
            if name == "ping_host":
                host = args.get("host", "192.168.70.1")
                return await self._get(f"/api/diagnostics/wifi/ping?host={host}")
            if name == "check_hailo":
                return await self._get("/api/diagnostics/hailo")
            if name == "get_camera_health":
                return await self._get("/api/camera/health")
            if name == "get_db_stats":
                return await self._get("/api/diagnostics/db")
            if name == "get_robot_capabilities":
                return await self._get("/api/robot/capabilities")
            if name == "get_alerts":
                return await self._get("/api/alerts/current")
            if name == "list_parameters":
                return await self._get("/api/config/schema")
            if name == "get_config":
                return await self._get("/api/config")
            if name == "set_parameters":
                patch = args.get("patch") or {}
                return await self._post("/api/config/tuning", patch)
            if name == "monitor_snapshot":
                status, diag, procs = await asyncio.gather(
                    self._get("/api/status"),
                    self._get("/api/diagnostics/full"),
                    self._get("/api/diagnostics/processes"),
                )
                return {"status": status, "diagnostics": diag, "processes": procs}
            if name == "get_service_status":
                return await self._get("/api/services")
            if name == "get_thermal_status":
                data = await self._get("/api/services")
                return {"thermal": data.get("thermal", {})}
            if name == "get_hardware_reference":
                return {"reference": ROVER2_KNOWLEDGE.strip()}
            if name == "describe_camera":
                prompt = args.get("prompt", "Describe what you see in front of the robot.")
                from urllib.parse import quote
                return await self._get(f"/api/vision/describe?prompt={quote(str(prompt))}")
            if name == "run_diagnostics":
                return await self._post("/api/diagnostics/full/run", {})
            if name == "set_robot_mode":
                mode = args.get("mode", "off")
                if mode == "follow":
                    return await self._post("/api/tracking", {"enabled": True})
                if mode == "detect":
                    return await self._post("/api/tracking", {"detect_only": True})
                return await self._post("/api/tracking", {"enabled": False, "detect_only": False})
            if name == "restart_service":
                return await self._post("/api/maintenance/restart-service", {})
            if name in ("stop_service", "start_service"):
                action = "stop" if name == "stop_service" else "start"
                return await self._post("/api/maintenance/service",
                                        {"action": action, "service": args.get("service", "")})
            if name == "reboot_pi":
                return await self._post("/api/maintenance/restart-pi", {})
            # ── New spoken-agent tools ──────────────────────────────────────
            if name == "introduce_rover":
                return {"ok": True}   # canned response handled by _ACTION_CANNED
            if name == "wave_arm":
                return await self._post("/api/arm/wave", {})
            if name == "get_full_diagnostics":
                status, diag, procs, services = await asyncio.gather(
                    self._get("/api/status"),
                    self._get("/api/diagnostics/full"),
                    self._get("/api/diagnostics/processes"),
                    self._get("/api/services"),
                )
                return {"status": status, "diagnostics": diag, "processes": procs,
                        "services": services}
            if name == "get_temperature_status":
                status, services = await asyncio.gather(
                    self._get("/api/status"),
                    self._get("/api/services"),
                )
                th = services.get("thermal", {})
                return {
                    "temp_c":           th.get("temp_c"),
                    "level":            th.get("level", "cool"),
                    "throttled":        th.get("throttle_current", False),
                    "hailo_ready":      status.get("hailo_ready", False),
                    "tracking_enabled": status.get("tracking_enabled", False),
                }
            if name == "get_hailo_status":
                status, hailo = await asyncio.gather(
                    self._get("/api/status"),
                    self._get("/api/diagnostics/hailo"),
                )
                services = await self._get("/api/services")
                return {
                    "hailo": hailo,
                    "person_detected": status.get("person_detected"),
                    "tracking_enabled": status.get("tracking_enabled"),
                    "temp_c": services.get("thermal", {}).get("temp_c"),
                }
            if name == "speak_diagnostics_summary":
                status, services = await asyncio.gather(
                    self._get("/api/status"),
                    self._get("/api/services"),
                )
                th = services.get("thermal", {})
                svcs = services.get("services", {})
                failed = [
                    k.replace(".service", "")
                    for k, v in svcs.items()
                    if isinstance(v, dict) and not v.get("active", True)
                ]
                return {
                    "temp_c": th.get("temp_c"),
                    "temp_level": th.get("level", "cool"),
                    "throttled": th.get("throttle_current", False),
                    "person_detected": status.get("person_detected", False),
                    "tracking_enabled": status.get("tracking_enabled", False),
                    "follow_mode": status.get("follow_mode", "off"),
                    "ble_seen": status.get("ble_seen", False),
                    "failed_services": failed,
                    "serial_ok": status.get("serial_ok", False),
                }
            if name == "suggest_fix":
                diag = await self._get("/api/diagnostics/full")
                services = await self._get("/api/services")
                ext = diag.get("extended", {})
                th = services.get("services", {})
                svcs_detail = services.get("services", {})
                fixes_applied: list[str] = []
                proposals: list[str] = []
                # Safe auto-fix: restart rover-camera if failed/inactive
                cam = svcs_detail.get("rover-camera.service", {})
                if isinstance(cam, dict) and not cam.get("active", True):
                    try:
                        await self._post("/api/maintenance/service",
                                         {"action": "start", "service": "rover-camera"})
                        fixes_applied.append("rover-camera restarted")
                    except Exception as e:
                        proposals.append(f"rover-camera restart failed: {e}")
                # Risky proposals only (no auto-apply)
                cpu = ext.get("cpu_percent", 0)
                if isinstance(cpu, (int, float)) and cpu > 85:
                    proposals.append(f"CPU at {cpu:.0f}% — consider stopping non-essential services")
                ram = ext.get("ram_used_mb", 0)
                if isinstance(ram, (int, float)) and ram > 380:
                    proposals.append(f"RAM at {ram:.0f} MB — consider restarting rover2-api")
                temps = ext.get("temperatures", {})
                for sensor, t in (temps.items() if isinstance(temps, dict) else []):
                    cur = t.get("current") if isinstance(t, dict) else None
                    if cur is not None and cur > 80:
                        proposals.append(f"{sensor} temperature {cur:.0f} C — reduce load")
                return {
                    "fixes_applied": fixes_applied,
                    "proposals": proposals,
                    "status": "ok" if not proposals else "action_needed",
                }
            if name == "control_follow_mode":
                action = args.get("action", "disable")
                if action in ("enable", "camera"):
                    return await self._post("/api/tracking",
                                            {"enabled": True, "ble_follow_enabled": False})
                if action == "fused":
                    return await self._post("/api/tracking",
                                            {"enabled": True, "ble_follow_enabled": True})
                if action == "detect":
                    return await self._post("/api/tracking",
                                            {"detect_only": True, "enabled": False})
                # disable
                return await self._post("/api/tracking",
                                        {"enabled": False, "detect_only": False,
                                         "ble_follow_enabled": False})
            # ── Home Assistant tools ────────────────────────────────────────
            if name in ("ha_get_status", "ha_get_temperatures", "ha_toggle"):
                ha = self._ha_client
                if ha is None:
                    return {"error": "HA not configured — set HA_TOKEN and HA_URL in /etc/rover2.env"}
                if name == "ha_get_temperatures":
                    sensors = await ha.get_temperature_sensors()
                    return {"sensors": sensors}
                if name == "ha_get_status":
                    toggleables, temps = await asyncio.gather(
                        ha.get_toggleable_entities(),
                        ha.get_temperature_sensors(),
                    )
                    return {"toggleables": toggleables, "temperatures": temps}
                if name == "ha_toggle":
                    entity_id = args.get("entity_id", "")
                    if not entity_id:
                        return {"error": "entity_id required"}
                    ok = await ha.toggle(entity_id)
                    return {"ok": ok, "entity_id": entity_id}
        except Exception as exc:
            return {"error": str(exc)}
        return {"error": f"unknown tool: {name}"}

    async def _get(self, path: str) -> Any:
        # verify=False: rover_base is loopback or LAN with self-signed cert — safe.
        async with httpx.AsyncClient(timeout=20, verify=False) as c:
            r = await c.get(f"{self._rover_base}{path}")
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=20, verify=False) as c:
            r = await c.post(f"{self._rover_base}{path}", json=body)
            return r.json()

    async def _call_model(self, messages: list[dict]) -> dict | None:
        self._last_model_error = None

        # hailo-ollama only supports /api/generate (not /api/chat or tools)
        if self._backend == "hailo":
            import re as _re
            flat_parts = [
                f"{m.get('role','user').capitalize()}: {_re.sub(r'\s+', ' ', m.get('content','') or '').strip()}"
                for m in messages
            ]
            flat_prompt = (" ".join(flat_parts) + " Assistant:").replace('\n', ' ').replace('\r', ' ')
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                    r = await c.post(
                        f"{self._base}/api/generate",
                        json={"model": self._model, "prompt": flat_prompt,
                              "stream": False, "options": {"num_predict": 300}},
                    )
                    if r.status_code == 200:
                        self._hailo_up = True
                        return {"message": {"content": r.json().get("response", "").strip(),
                                            "tool_calls": []}}
                    self._last_model_error = r.text[:200]
                    logger.warning("Agent: hailo /api/generate HTTP %d", r.status_code)
            except Exception as exc:
                self._last_model_error = str(exc)
                logger.warning("Agent: hailo _call_model error: %s", exc)
            self._hailo_up = False
            return None

        payload = {"model": self._model, "messages": messages,
                   "tools": _CPU_TOOLS, "stream": False}
        attempts = 1 if self._timeout_s >= 120 else 2
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                    r = await c.post(f"{self._base}/api/chat", json=payload)
                    if r.status_code == 200:
                        return r.json()
                    err_body = r.text[:300]
                    try:
                        err_json = r.json()
                        err_body = str(err_json.get("error", err_body))
                    except Exception:
                        pass
                    self._last_model_error = err_body
                    logger.warning("Agent: Ollama HTTP %d — %s", r.status_code, err_body)
                    if "does not support tools" in err_body.lower():
                        self._last_model_error = (
                            f"{self._model} does not support tools in Ollama. "
                            "Set agent.model to llama3.2:1b in config.yaml and run: ollama pull llama3.2:1b"
                        )
                        return None
                    if attempt + 1 < attempts and r.status_code in (500, 502, 503):
                        logger.info("Agent: retrying Ollama after HTTP %d", r.status_code)
                        await asyncio.sleep(5)
                        continue
                    return None
            except httpx.ConnectError:
                self._last_model_error = (
                    f"cannot connect to Ollama at {self._base} "
                    "(still starting after reboot? wait 30s and try again)"
                )
                logger.warning("Agent: %s", self._last_model_error)
                if attempt + 1 < attempts:
                    await asyncio.sleep(5)
                    await self.wait_for_ollama(30)
                    continue
                return None
            except httpx.TimeoutException:
                self._last_model_error = (
                    f"Ollama timed out after {int(self._timeout_s)}s "
                    "(model may still be loading after reboot — wait and try again)"
                )
                logger.warning("Agent: %s", self._last_model_error)
                if attempt + 1 < attempts:
                    logger.info("Agent: retrying Ollama after timeout")
                    await asyncio.sleep(5)
                    continue
                return None
            except Exception as exc:
                self._last_model_error = (str(exc) or type(exc).__name__)[:200]
                logger.warning("Agent: Ollama error: %s", self._last_model_error)
                return None
        return None

    async def _call_model_cpu(self, messages: list[dict]) -> dict | None:
        """CPU Ollama fallback for web chat — llama3.2:1b with full tool support.

        Used when hailo-ollama is down so web chat continues to work.
        Auto-recovers: next call to _call_model() that succeeds resets _hailo_up.
        """
        if not await self._ensure_cpu_ollama():
            self._last_model_error = (
                f"CPU Ollama ({_SPOKEN_CPU_BASE}) also unreachable — "
                "no LLM backend available"
            )
            return None
        payload = {
            "model":   self._cpu_model,
            "messages": messages,
            "tools":   _CPU_TOOLS,
            "stream":  False,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as c:
                r = await c.post(f"{_SPOKEN_CPU_BASE}/api/chat", json=payload)
                if r.status_code == 200:
                    logger.info(
                        "Agent: cpu fallback OK (hailo_up=%s, model=%s)",
                        self._hailo_up, self._cpu_model,
                    )
                    return r.json()
                self._last_model_error = r.text[:200]
                logger.warning("Agent: cpu fallback HTTP %d", r.status_code)
        except Exception as exc:
            self._last_model_error = (str(exc) or type(exc).__name__)[:200]
            logger.warning("Agent: cpu fallback error: %s", exc)
        return None
