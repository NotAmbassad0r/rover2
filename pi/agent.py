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
import re
import time
from typing import Any

import httpx

from agent_knowledge import ROVER2_KNOWLEDGE
from agent_solutions import build_solutions

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

_ALL_TOOLS     = _READ_TOOLS + _DANGEROUS_TOOLS
_DANGEROUS_NAMES = {t["function"]["name"] for t in _DANGEROUS_TOOLS}

_TOOL_NAMES = ", ".join(t["function"]["name"] for t in _ALL_TOOLS)

_SYSTEM_PROMPT = f"""You are ROVER2's onboard engineer: monitor hardware and software, change any allowed parameter, answer related questions.

TOOLS: {_TOOL_NAMES}

WORKFLOW:
1. Monitoring (CPU, temp, RAM, disk, serial, camera, follow, BLE): call monitor_snapshot() OR get_diagnostics() + get_top_processes().
2. Change settings: call list_parameters() then set_parameters(patch) with one or more sections.
3. Hardware/software facts (ports, IPs, wiring, services): call get_hardware_reference() and/or get_robot_capabilities().
4. Vision: describe_camera() for what the camera sees.
5. Robot control: set_robot_mode(follow|detect|off). Drive/grip/arm via status only unless user asks to change modes.

RULES:
- ALWAYS use tools for live data. Never invent temperatures, CPU %, or RSSI.
- Temperature: read system.temperature or extended temps from get_diagnostics — warn above 80 C, critical above 85 C.
- CPU high: name top process pid, name, cpu_pct from get_top_processes.
- After finding a fixable config issue, call set_parameters — do not only suggest edits.
- Dangerous actions (restart_service, reboot_pi, stop_service): use proposal only.
- Plain text, short paragraphs. No markdown.
- Always end with concrete suggested fixes (what to stop, restart, or change)."""

_MAX_TOOL_ROUNDS = 4
_DEFAULT_OLLAMA_TIMEOUT_S = 180

_SPOKEN_CPU_BASE = "http://127.0.0.1:11434"
_SPOKEN_MODEL = "llama3.2:1b"
_SPOKEN_TIMEOUT_S = 90.0  # allow for model load on first call; keep-alive prevents repeat waits
_KEEPALIVE_INTERVAL_S = 240  # 4 minutes — keeps model loaded between spoken requests

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
                f"{p.get('cpu_percent', '?')}% CPU, {p.get('memory_mb', '?')} MB"
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

    async def run_spoken_turn(self, user_text: str, lang: str = "en") -> str:
        """ROVER personality spoken response — bypasses fast-path and all tool use.

        Tries hailo-ollama first (if enabled in config), falls back to CPU ollama.
        """
        logger.info("Agent: run_spoken_turn called, user_text=%r, lang=%s", user_text[:50], lang)
        from voice_engine import ROVER_SYSTEM_PROMPT  # lazy import — avoids circular dep

        hailo_cfg = self._full_config.get("hailo_ollama", {})
        use_hailo = hailo_cfg.get("enabled", False)
        fallback_to_cpu = hailo_cfg.get("fallback_to_cpu", True)

        lang_note = (
            f"Respond in this language: {lang}. If lang is 'cs', respond in Czech. "
            if lang and lang not in ("en", "auto")
            else ""
        )

        if use_hailo:
            h_base  = hailo_cfg.get("host", "http://localhost:8000")
            h_model = hailo_cfg.get("model", "qwen2.5-instruct:1.5b")
            h_predict = int(hailo_cfg.get("num_predict", 120))
            h_temp    = float(hailo_cfg.get("temperature", 0.85))
            # hailo-ollama supports /api/chat when content has no newlines.
            # /api/generate cannot anchor ROVER persona in instruction-tuned qwen2.5-instruct:1.5b.
            # Flatten user_text to strip any accidental newlines from Whisper output.
            import re as _re
            def _flatten(s: str) -> str:
                return _re.sub(r"\s+", " ", s).strip()

            # Quick reachability check before sending — retry once after 5s if down
            async def _hailo_reachable() -> bool:
                try:
                    async with httpx.AsyncClient(timeout=3.0) as _c:
                        return (await _c.get(f"{h_base}/api/tags")).status_code == 200
                except Exception:
                    return False

            if not await _hailo_reachable():
                logger.info("Agent: hailo-ollama not ready — waiting 5s then retrying")
                await asyncio.sleep(5.0)
                if not await _hailo_reachable():
                    logger.warning("Agent: hailo-ollama unavailable — falling back to CPU")
                    if not fallback_to_cpu:
                        return ""
                    # skip hailo attempt, go straight to CPU path below
                else:
                    use_hailo = True  # reachable after retry — proceed

            if use_hailo:
                try:
                    async with httpx.AsyncClient(timeout=60.0) as c:
                        # /api/chat with flat (no-newline) content — hailo-ollama supports it.
                        # System + 3 few-shot prior turns anchor the ROVER persona reliably.
                        h_messages = (
                            [{"role": "system", "content": _ROVER_HAILO_SYSTEM}]
                            + _ROVER_HAILO_FEW_SHOT
                            + [{"role": "user", "content": _flatten(user_text)}]
                        )
                        r = await c.post(
                            f"{h_base}/api/chat",
                            json={
                                "model":    h_model,
                                "messages": h_messages,
                                "stream":   False,
                                "options":  {"num_predict": h_predict, "temperature": h_temp,
                                             "top_p": 0.92},
                            },
                        )
                        if r.status_code == 200:
                            reply = r.json().get("message", {}).get("content", "").strip()
                            logger.info("Agent: spoken turn OK (%d chars) via hailo", len(reply))
                            self._spoken_base  = h_base
                            self._spoken_model = h_model
                            self._start_keepalive()
                            return reply
                        logger.warning("Agent: hailo spoken turn HTTP %d — %s", r.status_code, r.text[:120])
                except Exception as exc:
                    logger.warning("Agent: hailo spoken turn error: %s", exc)
                if not fallback_to_cpu:
                    return ""

        # CPU ollama fallback (or primary when hailo disabled)
        user_msg = (
            f"{user_text}\n\n"
            f"(Respond in 2-4 spoken sentences. No formatting. "
            f"{lang_note}Respond in the same language the user spoke in: {lang}.)"
        )
        messages = [
            {"role": "system", "content": ROVER_SYSTEM_PROMPT.strip()},
            {"role": "user",   "content": user_msg},
        ]
        try:
            async with httpx.AsyncClient(timeout=_SPOKEN_TIMEOUT_S) as c:
                r = await c.post(
                    f"{_SPOKEN_CPU_BASE}/api/chat",
                    json={
                        "model":    _SPOKEN_MODEL,
                        "messages": messages,
                        "stream":   False,
                        "options":  {"num_predict": 120, "temperature": 0.85, "top_p": 0.92},
                    },
                )
                if r.status_code == 200:
                    reply = r.json().get("message", {}).get("content", "").strip()
                    logger.info("Agent: spoken turn OK (%d chars) via cpu", len(reply))
                    self._spoken_base  = _SPOKEN_CPU_BASE
                    self._spoken_model = _SPOKEN_MODEL
                    self._start_keepalive()
                    return reply
                logger.warning("Agent: cpu spoken turn HTTP %d — %s", r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("Agent: cpu spoken turn error: %s", exc)
        return ""

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        try:
            self._keepalive_task = asyncio.get_event_loop().create_task(self._keepalive_loop())
            logger.debug("Agent: Ollama keep-alive loop started")
        except Exception as exc:
            logger.debug("Agent: keepalive task start failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        """Ping the active spoken backend every 4 min to prevent model unloading."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    # hailo-ollama requires a prompt in /api/generate (no prompt-less keep_alive)
                    await c.post(
                        f"{self._spoken_base}/api/generate",
                        json={"model": self._spoken_model, "prompt": "hi",
                              "stream": False, "options": {"num_predict": 1},
                              "keep_alive": "10m"},
                    )
                logger.debug("Agent: keep-alive ping → %s", self._spoken_base)
            except Exception:
                pass

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

        for _round in range(_MAX_TOOL_ROUNDS):
            resp = await self._call_model(conv)
            if resp is None:
                if self._backend == "hailo":
                    hint = (
                        "hailo-ollama on AI HAT+ not reachable. "
                        "Run: sudo bash /opt/rover2/scripts/setup_ai_on_hailo.sh "
                        "(stops CPU ollama, starts hailo-ollama). "
                        "Turn FOLLOW off if the HAT is busy."
                    )
                else:
                    hint = (
                        f"Ollama unreachable or model {self._model} missing. "
                        "On the Pi: systemctl status ollama && ollama list"
                    )
                detail = self._last_model_error or hint
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
        return {
            "backend": self._backend,
            "base": self._base or None,
            "model": self._model,
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
        except Exception as exc:
            return {"error": str(exc)}
        return {"error": f"unknown tool: {name}"}

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{self._rover_base}{path}")
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=20) as c:
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
                        return {"message": {"content": r.json().get("response", "").strip(),
                                            "tool_calls": []}}
                    self._last_model_error = r.text[:200]
                    logger.warning("Agent: hailo /api/generate HTTP %d", r.status_code)
            except Exception as exc:
                self._last_model_error = str(exc)
                logger.warning("Agent: hailo _call_model error: %s", exc)
            return None

        payload = {"model": self._model, "messages": messages,
                   "tools": _ALL_TOOLS, "stream": False}
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
