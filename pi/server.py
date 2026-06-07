"""ROVER2 FastAPI server — motors, gripper, ultrasonic, static D-pad UI."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from arm_control import arm_pwm
from body_tracker import BodyTracker
from megapi import MegaPiBridge

try:
    from camera_proxy import check_camera_up, snapshot_response, stream_response

    _CAMERA_PROXY = True
except ImportError:
    _CAMERA_PROXY = False

try:
    from camera_idle import CameraIdleManager

    _CAMERA_IDLE = True
except ImportError:
    _CAMERA_IDLE = False

try:
    from thermal import ThermalMonitor, read_thermal, read_cpu_freq_mhz

    _THERMAL = True
except ImportError:
    _THERMAL = False
from config_public import public_config
from config_runtime import apply_config_patch
from config_store import (
    TUNING_SECTIONS,
    extract_tuning_patch,
    merge_tuning,
    save_config,
    tuning_schema_public,
)
from diagnostics import SCRIPT_CATALOG, gather_diagnostics, gather_full_diagnostics, run_wifi_restore as _wifi_restore_privileged
from log_buffer import get_log_handler
import metrics_store as _mstore
from safety import SafetyMonitor
from agent import RoverAgent
from vlm_engine import VLMEngine
from ws_control import ControlHub, build_telemetry

try:
    from guard import GuardController as _GuardController
    _GUARD = True
except ImportError:
    _GUARD = False

try:
    from watchdog import Watchdog as _Watchdog
    _WATCHDOG = True
except ImportError:
    _WATCHDOG = False

try:
    from audio_router import AudioRouter as _AudioRouter
    _AUDIO_ROUTER = True
except ImportError:
    _AUDIO_ROUTER = False

try:
    import voice_engine as _voice_engine
    _VOICE = True
except ImportError:
    _VOICE = False

logger = logging.getLogger(__name__)
_START = time.monotonic()

# Closing phrases — trigger immediate session end after the reply is spoken on A32
_CLOSING_PATTERNS: re.Pattern = re.compile(
    r"\b(goodbye|good\s*bye|bye|see\s+you|see\s+ya|thank\s+you|thanks|"
    r"have\s+a\s+good\s+(one|day|night|evening)|good\s*night|goodnight|"
    r"farewell|that.{0,5}(all|enough)|take\s+care)\b",
    re.IGNORECASE,
)

# Fuzzy wake-word set — faster-whisper mishearings of Czech/German-accented "rover"
_WAKE_WORDS: frozenset[str] = frozenset({
    # Core matches
    "rover", "robo", "robot", "over", "rove", "mover", "dover", "lover",
    # German-accent mishearings: 'v' → 'w', 'v' → 'f', vowel-shift
    "rower", "rofer", "roffer", "rofar",
})
_STATIC = Path(__file__).parent / "web" / "static"
_FACE  = Path(__file__).parent / "face"

# (left_sign, right_sign) before firmware negation — tuned for Ultimate 2.0 wiring.
# Override in config.yaml → drive.direction_map if your chassis differs.
_DIRECTIONS: dict[str, tuple[int, int]] = {
    "forward": (1, -1),
    "back": (-1, 1),
    "left": (1, 1),
    "right": (-1, -1),
    "stop": (0, 0),
}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config.yaml must be a mapping")
    return data


def _scale(speed_fraction: float, max_speed: int) -> int:
    return int(max(-max_speed, min(max_speed, round(speed_fraction * max_speed))))


def _public_ultrasonic_cm(raw: int | None, max_age_s: float = 2.0, age_s: float | None = None) -> int | None:
    """Map bridge cache to API: null if stale, no echo, or never read."""
    if raw is None or age_s is None or age_s > max_age_s:
        return None
    if raw < 0:
        return None
    return raw


def _directions_from_config(config: dict[str, Any]) -> dict[str, tuple[int, int]]:
    custom = config.get("drive", {}).get("direction_map")
    if not isinstance(custom, dict):
        return dict(_DIRECTIONS)
    merged = dict(_DIRECTIONS)
    for key, pair in custom.items():
        if not isinstance(key, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        merged[key.lower()] = (int(pair[0]), int(pair[1]))
    return merged


def create_app(
    megapi: MegaPiBridge,
    config: dict[str, Any],
    safety_monitor: SafetyMonitor | None = None,
    body_tracker: BodyTracker | None = None,
    vlm_engine: VLMEngine | None = None,
    rover_agent: RoverAgent | None = None,
    guard_controller: Any | None = None,
) -> FastAPI:
    drive_cfg = config.get("drive", {})
    default_speed = int(drive_cfg.get("default_speed", 120))
    max_speed = int(drive_cfg.get("max_speed", 255))
    directions = _directions_from_config(config)
    ws_cfg = config.get("websocket", {})
    telemetry_interval_s = float(ws_cfg.get("telemetry_interval_s", 0.4))
    cam_cfg = config.get("camera", {})
    camera_stream_url = str(cam_cfg.get("upstream_url", "http://127.0.0.1:8081/stream"))
    camera_snapshot_url = str(cam_cfg.get("snapshot_url", "http://127.0.0.1:8081/snapshot"))
    camera_health_url = str(cam_cfg.get("health_url", "http://127.0.0.1:8081/health"))

    cam_idle = CameraIdleManager(config) if _CAMERA_IDLE else None
    thermal_monitor = ThermalMonitor() if _THERMAL else None

    hub = ControlHub(
        megapi=megapi,
        directions=directions,
        max_speed=max_speed,
        safety_monitor=safety_monitor,
        start_monotonic=_START,
        public_ultrasonic_cm=_public_ultrasonic_cm,
        telemetry_interval_s=telemetry_interval_s,
        body_tracker=body_tracker,
        config=config,
        cam_idle=cam_idle,
        guard_controller=guard_controller,
    )

    # Configure voice engine from runtime config (voice model, length_scale, sox)
    if _VOICE:
        _voice_engine.configure(config)

    # Audio router — lazy construction (skips if audio_router module not installed)
    _audio_cfg = config.get("audio_routing", {})
    audio_router = _AudioRouter(_audio_cfg) if _AUDIO_ROUTER else None  # type: ignore[assignment]

    # Wire guard speak function now that audio_router is available
    if guard_controller is not None and audio_router is not None and _VOICE:
        def _guard_speak_fn(event_key: str) -> None:
            _voice_engine.speak_event(event_key, audio_router=audio_router)
        guard_controller.set_speak_fn(_guard_speak_fn)

    # Watchdog — self-healing monitor (asyncio task, no separate service)
    watchdog = _Watchdog(  # type: ignore[assignment]
        config,
        audio_router=audio_router,
        body_tracker=body_tracker,
        safety_monitor=safety_monitor,
        megapi=megapi,
    ) if _WATCHDOG else None

    app = FastAPI(title="ROVER2", version="2.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    if _FACE.exists():
        app.mount("/face", StaticFiles(directory=_FACE, html=True), name="face")
    config_path = Path(__file__).parent / "config.yaml"

    import collections as _collections

    _diag_cache: dict[str, Any] | None = None
    _diag_cache_time: float = 0.0
    _DIAG_TTL_S = 30.0
    _full_diag_cache: dict[str, Any] | None = None
    _full_diag_cache_time: float = 0.0
    _FULL_DIAG_TTL_S = 10.0

    # Fault history ring buffer — clears on service restart (intentional)
    _fault_history: _collections.deque = _collections.deque(maxlen=20)

    def _add_fault(component: str, msg: str, action: str = "") -> None:
        _fault_history.append({
            "ts": int(time.time()),
            "component": component,
            "msg": msg,
            "action": action,
        })
        logger.warning("FAULT [%s] %s%s", component, msg, f" → {action}" if action else "")

    # Fault escalation cooldown/counter state
    _ultra_reconnect_at: float = 0.0
    _hailo_fault_since: float | None = None
    _hailo_recovery_count: int = 0
    _hailo_recovery_at: float = 0.0
    _camera_restart_at: float = 0.0
    _ollama_unresponsive_count: int = 0
    _mem_warn_ts: float = 0.0
    # Transient service alerts — set by /api/services, included in next WS push
    _camera_alert: dict | None = None
    _ollama_alert: dict | None = None

    _metrics_cfg = config.get("metrics", {})
    _METRICS_INTERVAL_S = float(_metrics_cfg.get("interval_s", 5.0))
    _METRICS_INTERVAL_HOT_S = float(_metrics_cfg.get("interval_hot_s", 15.0))
    _METRICS_CPU_HOT_PCT = float(_metrics_cfg.get("cpu_hot_threshold", 70.0))
    _METRICS_RETENTION_DAYS = int(_metrics_cfg.get("retention_days", 7))
    _PURGE_INTERVAL = max(1, int(3600 / _METRICS_INTERVAL_S))  # purge ~once/hour

    _mem_cfg = config.get("memory_watchdog", {})
    _MEM_TARGET_MB = float(_mem_cfg.get("target_mb", 300))
    _MEM_WARN_MB = float(_mem_cfg.get("warn_mb", 280))
    _MEM_CRITICAL_MB = float(_mem_cfg.get("critical_mb", 350))
    _MEM_EMERGENCY_MB = float(_mem_cfg.get("emergency_mb", 400))
    _MEM_WARN_COOLDOWN_S = float(_mem_cfg.get("warn_cooldown_s", 1800))
    _MEM_CRITICAL_COOLDOWN_S = float(_mem_cfg.get("critical_cooldown_s", 300))

    # Alert state — updated each metrics tick, pushed via WebSocket extra
    _active_alerts: list[dict] = []
    _cpu_high_since: float | None = None
    _thermal_alert: dict | None = None  # set by /api/services on-demand evaluation

    # AP state — cached 10 s (subprocess-based, never run every 400 ms)
    _ap_state_cache: str = ""
    _ap_state_cache_ts: float = 0.0
    _AP_STATE_TTL_S: float = 10.0

    # Proactive speech transition tracking
    _prev_person_detected: bool = False
    _prev_tracking_active: bool = False
    _prev_forward_blocked: bool = False
    _prev_thermal_level: str = "cool"
    _prev_camera_sleeping: bool = False

    # Voice conversation state — managed by /api/voice/converse + /api/voice/converse/end
    _conversation_active: bool = False
    _tracking_was_enabled: bool = False
    _conversation_timeout_task: asyncio.Task | None = None

    async def _hailo_recovery_task() -> None:
        if body_tracker is None:
            return
        st_before = body_tracker.get_state()
        was_enabled = st_before.get("enabled", False)
        was_detect = st_before.get("detect_only", False)
        logger.warning("Hailo recovery: disabling follow/detect for 5s")
        body_tracker.set_enabled(False)
        body_tracker.set_detect_only(False)
        await asyncio.sleep(5.0)
        if was_enabled:
            body_tracker.set_enabled(True)
        elif was_detect:
            body_tracker.set_detect_only(True)
        logger.warning("Hailo recovery complete (was_enabled=%s, was_detect=%s)", was_enabled, was_detect)

    _THRESHOLDS = [
        ("temp_cpu_thermal", 85, "crit", "CPU temperature CRITICAL {v:.1f} C — overheating"),
        ("temp_cpu_thermal", 78, "warn", "CPU temperature {v:.1f} C — running hot"),
        ("temp_rp1_adc",     75, "warn", "RP1 chip {v:.1f} C — elevated"),
        ("ram_percent",      85, "warn", "RAM {v:.0f}% — high memory usage"),
        ("disk_percent",     80, "warn", "Disk {v:.0f}% full — running low on space"),
    ]

    async def _metrics_loop() -> None:
        import os as _os
        purge_tick = 0
        _prev_net: dict = {}
        _prev_disk: tuple[int, int] | None = None
        _prev_t = time.monotonic()
        _my_proc = None

        _sleep_s = _METRICS_INTERVAL_S
        while True:
            await asyncio.sleep(_sleep_s)
            now = time.monotonic()
            elapsed = max(now - _prev_t, 0.1)
            _prev_t = now
            try:
                points: dict[str, float | None] = {}
                try:
                    import psutil

                    # System
                    points["cpu_percent"]  = psutil.cpu_percent()
                    vm = psutil.virtual_memory()
                    points["ram_percent"]  = vm.percent
                    points["ram_used_mb"]  = vm.used / (1024 ** 2)
                    sw = psutil.swap_memory()
                    points["swap_percent"] = sw.percent
                    points["disk_percent"] = psutil.disk_usage("/").percent

                    # Temperatures
                    for sensor, entries in (psutil.sensors_temperatures() or {}).items():
                        for e in entries:
                            key = f"temp_{sensor}_{e.label}" if e.label else f"temp_{sensor}"
                            points[key] = e.current

                    # Network rates (bytes/s per interface)
                    net = psutil.net_io_counters(pernic=True)
                    for iface in ("eth0", "wlan0"):
                        if iface in net:
                            c = net[iface]
                            if iface in _prev_net:
                                p = _prev_net[iface]
                                points[f"net_{iface}_tx_bps"] = (c.bytes_sent - p.bytes_sent) / elapsed
                                points[f"net_{iface}_rx_bps"] = (c.bytes_recv - p.bytes_recv) / elapsed
                            _prev_net[iface] = c

                    # Disk I/O rates
                    dio = psutil.disk_io_counters()
                    if dio:
                        if _prev_disk:
                            points["disk_read_bps"]  = (dio.read_bytes  - _prev_disk[0]) / elapsed
                            points["disk_write_bps"] = (dio.write_bytes - _prev_disk[1]) / elapsed
                        _prev_disk = (dio.read_bytes, dio.write_bytes)

                    # rover2-api process
                    if _my_proc is None:
                        _my_proc = psutil.Process(_os.getpid())
                        _my_proc.cpu_percent()  # prime
                    with _my_proc.oneshot():
                        points["process_cpu_percent"] = _my_proc.cpu_percent()
                        points["process_memory_mb"]   = _my_proc.memory_info().rss / (1024 ** 2)

                except ImportError:
                    pass

                # Robot state
                if body_tracker is not None:
                    st = body_tracker.get_state()
                    points["hailo_ready"]     = 1.0 if st.get("hailo_ready") else 0.0
                    points["follow_enabled"]  = 1.0 if st.get("enabled") else 0.0
                    points["detect_only"]     = 1.0 if st.get("detect_only") else 0.0
                    points["person_detected"] = 1.0 if st.get("person_detected") else 0.0
                    # follow_state: 0=off 1=detect 2=camera 3=fused 4=ble
                    if st.get("enabled"):
                        _fm = st.get("follow_mode", "fused")
                        if _fm == "ble":
                            points["follow_state"] = 4.0
                        elif _fm == "camera":
                            points["follow_state"] = 2.0
                        else:  # fused (default)
                            points["follow_state"] = 3.0
                    elif st.get("detect_only"):
                        points["follow_state"] = 1.0
                    else:
                        points["follow_state"] = 0.0
                    ble = st.get("ble") or {}
                    points["ble_seen"] = 1.0 if ble.get("seen") else 0.0
                    if ble.get("rssi") is not None:
                        points["ble_rssi"] = float(ble["rssi"])

                if safety_monitor is not None:
                    d = safety_monitor.distance_cm
                    if d is not None and 0 < d < 400:
                        points["ultrasonic_cm"] = float(d)
                    points["safety_blocked"] = 1.0 if safety_monitor.forward_blocked else 0.0

                if cam_idle is not None:
                    points["camera_sleeping"] = 1.0 if cam_idle.sleeping else 0.0

                await asyncio.to_thread(_mstore.write, points)

                # ── Threshold alert evaluation ──────────────────────────────
                nonlocal _active_alerts, _cpu_high_since, _thermal_alert
                nonlocal _ultra_reconnect_at
                nonlocal _hailo_fault_since, _hailo_recovery_count, _hailo_recovery_at
                nonlocal _mem_warn_ts
                nonlocal _ap_state_cache, _ap_state_cache_ts
                new_alerts: list[dict] = []
                seen_metrics: set[str] = set()
                for metric, threshold, severity, tmpl in _THRESHOLDS:
                    if metric in seen_metrics:
                        continue
                    v = points.get(metric)
                    if v is not None and v >= threshold:
                        new_alerts.append({
                            "metric": metric, "value": round(v, 1),
                            "severity": severity, "ts": int(time.time()),
                            "msg": tmpl.format(v=v),
                        })
                        seen_metrics.add(metric)
                # Sustained CPU (>85% for >30s)
                cpu_v = points.get("cpu_percent")
                if cpu_v is not None:
                    if cpu_v >= 85:
                        if _cpu_high_since is None:
                            _cpu_high_since = time.monotonic()
                        elif time.monotonic() - _cpu_high_since >= 30:
                            new_alerts.append({
                                "metric": "cpu_percent", "value": round(cpu_v, 1),
                                "severity": "warn", "ts": int(time.time()),
                                "msg": f"CPU {cpu_v:.0f}% — sustained high load",
                            })
                    else:
                        _cpu_high_since = None

                # Ultrasonic fault escalation
                ultra_faults = megapi.ultrasonic_fault_count
                now_m = time.monotonic()
                if ultra_faults >= 10:
                    if now_m - _ultra_reconnect_at >= 60.0:
                        _ultra_reconnect_at = now_m
                        _add_fault("ultrasonic", f"Serial timeout x{ultra_faults} — reconnecting serial", "serial reconnect")
                        megapi.trigger_reconnect()
                elif ultra_faults >= 3:
                    new_alerts.append({
                        "metric": "ultrasonic_fault_count", "value": ultra_faults,
                        "severity": "warn", "ts": int(time.time()),
                        "msg": f"Ultrasonic unresponsive — serial may be wedged ({ultra_faults} timeouts)",
                    })

                # Hailo fault escalation
                if body_tracker is not None:
                    st_h = body_tracker.get_state()
                    is_active = st_h.get("enabled") or st_h.get("detect_only")
                    hailo_rdy = st_h.get("hailo_ready", False)
                    past_warmup = body_tracker.is_past_warmup()
                    if is_active and not hailo_rdy and past_warmup:
                        if _hailo_fault_since is None:
                            _hailo_fault_since = now_m
                        fault_dur = now_m - _hailo_fault_since
                        if fault_dur >= 60.0 and _hailo_recovery_count < 2:
                            if now_m - _hailo_recovery_at >= 30.0:
                                _hailo_recovery_count += 1
                                _hailo_recovery_at = now_m
                                _add_fault(
                                    "hailo",
                                    f"Hailo unavailable >{fault_dur:.0f}s — auto-recovery attempt {_hailo_recovery_count}/2",
                                    "auto-recovery",
                                )
                                asyncio.create_task(_hailo_recovery_task())
                        elif _hailo_recovery_count >= 2 and now_m - _hailo_recovery_at >= 90.0:
                            _hailo_recovery_at = now_m
                            _add_fault("hailo", "Hailo unavailable after 2 recovery attempts — manual restart needed", "alert")
                            new_alerts.append({
                                "metric": "hailo_ready", "value": 0,
                                "severity": "crit", "ts": int(time.time()),
                                "msg": "Hailo unavailable after recovery — restart rover2-api manually",
                            })
                    else:
                        if hailo_rdy and _hailo_fault_since is not None:
                            _hailo_fault_since = None
                            _hailo_recovery_count = 0
                        elif not is_active:
                            _hailo_fault_since = None

                # Memory watchdog
                proc_mem = points.get("process_memory_mb")
                if proc_mem is not None:
                    if proc_mem > _MEM_EMERGENCY_MB:
                        new_alerts.append({
                            "metric": "process_memory_mb", "value": round(proc_mem, 1),
                            "severity": "crit", "ts": int(time.time()),
                            "msg": f"rover2-api RSS {proc_mem:.0f} MB — emergency (budget: {_MEM_TARGET_MB:.0f} MB)",
                        })
                        _add_fault("memory", f"RSS {proc_mem:.0f} MB — emergency", "alert")
                        _mem_warn_ts = now_m
                        if _VOICE and audio_router is not None:
                            _voice_engine.speak_event("MEMORY_CRITICAL", {}, audio_router)
                    elif proc_mem > _MEM_CRITICAL_MB:
                        if now_m - _mem_warn_ts >= _MEM_CRITICAL_COOLDOWN_S:
                            new_alerts.append({
                                "metric": "process_memory_mb", "value": round(proc_mem, 1),
                                "severity": "crit", "ts": int(time.time()),
                                "msg": f"rover2-api RSS {proc_mem:.0f} MB — critical (budget: {_MEM_TARGET_MB:.0f} MB)",
                            })
                            _add_fault("memory", f"RSS {proc_mem:.0f} MB — critical", "alert")
                            _mem_warn_ts = now_m
                            if _VOICE and audio_router is not None:
                                _voice_engine.speak_event("MEMORY_CRITICAL", {}, audio_router)
                    elif proc_mem > _MEM_WARN_MB:
                        if now_m - _mem_warn_ts >= _MEM_WARN_COOLDOWN_S:
                            new_alerts.append({
                                "metric": "process_memory_mb", "value": round(proc_mem, 1),
                                "severity": "warn", "ts": int(time.time()),
                                "msg": f"rover2-api RSS {proc_mem:.0f} MB — approaching budget ({_MEM_TARGET_MB:.0f} MB)",
                            })
                            _add_fault("memory", f"RSS {proc_mem:.0f} MB — above target", "alert")
                            _mem_warn_ts = now_m
                            if _VOICE and audio_router is not None:
                                _voice_engine.speak_event("MEMORY_WARN", {}, audio_router)

                # Ease Pi thermals: sample less often when CPU is already high
                if cpu_v is not None and cpu_v >= _METRICS_CPU_HOT_PCT:
                    _sleep_s = _METRICS_INTERVAL_HOT_S
                else:
                    _sleep_s = _METRICS_INTERVAL_S
                _active_alerts = new_alerts
                all_alerts = list(new_alerts)
                if _thermal_alert:
                    all_alerts.append(_thermal_alert)
                if _camera_alert:
                    all_alerts.append(_camera_alert)
                if _ollama_alert:
                    all_alerts.append(_ollama_alert)
                if watchdog is not None:
                    all_alerts.extend(watchdog.get_persistent_alerts())
                # Proactive speech event detection
                nonlocal _prev_person_detected, _prev_tracking_active
                nonlocal _prev_forward_blocked, _prev_thermal_level, _prev_camera_sleeping
                if _VOICE and audio_router is not None:
                    if body_tracker is not None:
                        _st2 = body_tracker.get_state()
                        _pd  = bool(_st2.get("person_detected"))
                        _ta  = bool(_st2.get("enabled")) or bool(_st2.get("detect_only"))
                        if _pd and not _prev_person_detected and _ta:
                            _voice_engine.speak_event("PERSON_FOUND", {}, audio_router)
                        elif not _pd and _prev_person_detected and _ta:
                            _voice_engine.speak_event("PERSON_LOST", {}, audio_router)
                        _prev_person_detected = _pd
                        _prev_tracking_active = _ta
                    if safety_monitor is not None:
                        _fb = safety_monitor.forward_blocked
                        if _fb and not _prev_forward_blocked:
                            if body_tracker is not None and body_tracker.enabled:
                                _voice_engine.speak_event("OBSTACLE_DETECTED", {}, audio_router)
                        _prev_forward_blocked = _fb
                    _t_temp = points.get("temp_cpu_thermal")
                    if _t_temp is not None:
                        _t_level = "crit" if _t_temp >= 80 else ("warn" if _t_temp >= 70 else "cool")
                        if _t_level != _prev_thermal_level:
                            if _t_level == "warn":
                                _voice_engine.speak_event("THERMAL_WARN", {}, audio_router)
                            elif _t_level == "crit":
                                _voice_engine.speak_event("THERMAL_CRITICAL", {}, audio_router)
                        _prev_thermal_level = _t_level
                    if cam_idle is not None:
                        _cs = cam_idle.sleeping
                        if _cs and not _prev_camera_sleeping:
                            _voice_engine.speak_event("CAMERA_SLEEPING", {}, audio_router)
                        _prev_camera_sleeping = _cs

                # AP state — cached 10 s (nmcli + systemctl subprocesses)
                _now_ap = time.monotonic()
                if _now_ap - _ap_state_cache_ts >= _AP_STATE_TTL_S:
                    try:
                        _proc = await asyncio.create_subprocess_exec(
                            "nmcli", "-t", "-f", "DEVICE,STATE", "device",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        _out, _ = await asyncio.wait_for(_proc.communicate(), timeout=5.0)
                        if b"wlan0:connected" in _out:
                            _ap_state_cache = "home"
                        else:
                            _proc2 = await asyncio.create_subprocess_exec(
                                "systemctl", "is-active", "hostapd",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            _out2, _ = await asyncio.wait_for(_proc2.communicate(), timeout=5.0)
                            _ap_state_cache = "away" if _out2.strip() == b"active" else "error"
                        _ap_state_cache_ts = _now_ap
                    except Exception:
                        _ap_state_cache = "error"

                # Watchdog telemetry fields
                _wd_last_action = watchdog.last_action if watchdog is not None else ""
                _wd_last_action_ts = watchdog.last_action_ts if watchdog is not None else 0.0
                _wd_last_cycle = watchdog.last_cycle_iso if watchdog is not None else ""

                hub.set_telemetry_extra({
                    "alerts": all_alerts,
                    "ws_client_count": hub.client_count,
                    "audio_mode": audio_router.get_mode() if audio_router else None,
                    "conversation_active": _conversation_active,
                    "ap_state": _ap_state_cache,
                    "watchdog_last_action": _wd_last_action,
                    "watchdog_last_action_ts": _wd_last_action_ts,
                    "watchdog_last_cycle": _wd_last_cycle,
                })
                # ───────────────────────────────────────────────────────────

                purge_tick += 1
                if purge_tick >= _PURGE_INTERVAL:
                    await asyncio.to_thread(_mstore.purge, _METRICS_RETENTION_DAYS)
                    purge_tick = 0
            except Exception as exc:
                logger.debug("metrics_loop error: %s", exc)

    async def _boot_speech_task() -> None:
        await asyncio.sleep(30.0)
        if _VOICE and audio_router is not None:
            _voice_engine.speak_event("BOOT_COMPLETE", {}, audio_router)

    async def _warmup_hailo(max_attempts: int = 5, delay_s: float = 10.0) -> None:
        """Retry warmup ping to hailo-ollama until it responds or attempts exhausted."""
        import httpx as _httpx
        hailo_cfg = config.get("hailo_ollama", {})
        if not hailo_cfg.get("enabled", False):
            return
        h_base  = hailo_cfg.get("host", "http://localhost:8000")
        h_model = hailo_cfg.get("model", "qwen2.5-instruct:1.5b")
        for attempt in range(1, max_attempts + 1):
            try:
                async with _httpx.AsyncClient(timeout=10.0) as c:
                    # Use a flat single-word prompt — newlines crash hailo-ollama oatpp tokenizer
                    r = await c.post(
                        f"{h_base}/api/generate",
                        json={"model": h_model, "prompt": "hi",
                              "stream": False, "options": {"num_predict": 1}},
                    )
                    if r.status_code == 200:
                        logger.info("hailo-ollama: warmup OK on attempt %d/%d", attempt, max_attempts)
                        if rover_agent is not None:
                            rover_agent._available = True
                            rover_agent._hailo_up  = True
                            rover_agent._last_availability_check = time.monotonic()
                        return
                    logger.debug("hailo-ollama: warmup HTTP %d on attempt %d", r.status_code, attempt)
            except Exception as exc:
                logger.info("hailo-ollama: warmup attempt %d/%d: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                await asyncio.sleep(delay_s)
        logger.warning(
            "hailo-ollama: warmup failed after %d attempts — CPU fallback active until it responds",
            max_attempts,
        )
        if rover_agent is not None:
            rover_agent._hailo_up = False

    @app.on_event("startup")
    async def _start_metrics() -> None:
        if body_tracker is not None and body_tracker._detect_only_on_start:
            body_tracker.set_detect_only(True)
            logger.info("BodyTracker: detect-only enabled on start (config)")
        asyncio.create_task(_metrics_loop())
        if rover_agent is not None:
            asyncio.create_task(rover_agent.warmup())
            # Preload llama3.2:1b for spoken turns after 60 s (lets hailo settle first)
            asyncio.create_task(rover_agent.warmup_spoken_model(delay_s=60.0))
        asyncio.create_task(_warmup_hailo(max_attempts=5, delay_s=10.0))
        if cam_idle is not None:
            _ultra_poll_s = float(config.get("ultrasonic", {}).get("poll_interval_s", 0.5))
            asyncio.create_task(cam_idle.run(megapi, body_tracker, _ultra_poll_s))
        if audio_router is not None:
            await audio_router.start()
            asyncio.create_task(_boot_speech_task())
        if guard_controller is not None:
            await guard_controller.start()
        if watchdog is not None:
            watchdog.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if audio_router is not None:
            await audio_router.stop()
        if guard_controller is not None:
            await guard_controller.stop()
        if watchdog is not None:
            watchdog.stop()

    @app.websocket("/ws")
    async def websocket_control(websocket: WebSocket) -> None:
        await websocket.accept()
        await hub.serve(websocket)

    @app.websocket("/ws/audio")
    async def websocket_audio(websocket: WebSocket) -> None:
        """Binary PCM stream for A32 PWA audio playback (TTS frames from audio_router)."""
        await websocket.accept()
        if audio_router is not None:
            audio_router.add_ws_audio_client(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep alive; client may send nothing
        except Exception:
            pass
        finally:
            if audio_router is not None:
                audio_router.remove_ws_audio_client(websocket)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            _STATIC / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/rover-ca.crt")
    async def rover_ca_cert() -> Response:
        """Serve the ROVER CA certificate for installation on client devices.
        Download this once on A32 to eliminate cert warnings permanently."""
        ca_path = Path("/opt/rover2/rover-ca.crt")
        if not ca_path.exists():
            raise HTTPException(status_code=404, detail="CA cert not found — run cert generation script on Pi")
        return Response(
            content=ca_path.read_bytes(),
            media_type="application/x-x509-ca-cert",
            headers={
                "Content-Disposition": "attachment; filename=rover-ca.crt",
                "Cache-Control": "no-cache",
            },
        )

    if _CAMERA_PROXY:

        @app.get("/stream")
        async def camera_stream() -> StreamingResponse:
            """MJPEG proxy — same feed as rover-camera on port 8081."""
            if cam_idle is not None:
                cam_idle.notify_stream_connect()
            await check_camera_up(camera_health_url)
            return await stream_response(camera_stream_url)

        @app.get("/snapshot")
        async def camera_snapshot() -> Response:
            return await snapshot_response(camera_snapshot_url)

        @app.get("/api/camera/health")
        async def camera_health() -> JSONResponse:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    upstream = await client.get(camera_health_url)
            except httpx.HTTPError as exc:
                return JSONResponse({"status": "error", "detail": str(exc)[:120]})
            ok = upstream.status_code == 200 and upstream.text.strip() == "ok"
            return JSONResponse({
                "status": "ok" if ok else "no_frame",
                "upstream": camera_health_url,
                "upstream_code": upstream.status_code,
            })
    else:

        @app.get("/stream")
        async def camera_stream_unavailable() -> JSONResponse:
            return JSONResponse(
                {"detail": "camera proxy requires httpx (pip install httpx)"},
                status_code=503,
            )

        @app.get("/api/camera/health")
        async def camera_health_unavailable() -> JSONResponse:
            return JSONResponse({"status": "error", "detail": "httpx not installed"})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/scripts")
    async def list_scripts() -> JSONResponse:
        """Maintenance script catalog (dev copy-paste + safe Pi API actions)."""
        return JSONResponse({"scripts": SCRIPT_CATALOG})

    @app.get("/api/diagnostics")
    async def get_diagnostics() -> JSONResponse:
        nonlocal _diag_cache, _diag_cache_time
        now = time.monotonic()
        if _diag_cache is None or (now - _diag_cache_time) > _DIAG_TTL_S:
            _diag_cache = await asyncio.to_thread(
                gather_diagnostics,
                megapi,
                safety_monitor=safety_monitor,
                body_tracker=body_tracker,
                camera_health_url=camera_health_url,
                api_port=int(config.get("server", {}).get("port", 8082)),
            )
            _diag_cache_time = time.monotonic()
        return JSONResponse(_diag_cache)

    @app.post("/api/diagnostics/run")
    async def run_diagnostics() -> JSONResponse:
        nonlocal _diag_cache, _diag_cache_time
        try:
            report = await asyncio.wait_for(
                asyncio.to_thread(
                    gather_diagnostics,
                    megapi,
                    safety_monitor=safety_monitor,
                    body_tracker=body_tracker,
                    camera_health_url=camera_health_url,
                    api_port=int(config.get("server", {}).get("port", 8082)),
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            report = dict(_diag_cache or {})
            report["timeout"] = True
        _diag_cache = report
        _diag_cache_time = time.monotonic()
        return JSONResponse(report)

    @app.get("/api/diagnostics/processes")
    async def get_top_processes() -> JSONResponse:
        """Top 12 processes by CPU%, sampled over 0.5s. Used by the agent for diagnosis."""
        def _collect() -> list[dict]:
            try:
                import psutil
                # Prime all processes
                procs = list(psutil.process_iter(["pid", "name", "status"]))
                for p in procs:
                    try:
                        p.cpu_percent()
                    except Exception:
                        pass
                import time as _t
                _t.sleep(0.5)
                rows = []
                for p in procs:
                    try:
                        cpu = p.cpu_percent()
                        mem = p.memory_info().rss // (1024 * 1024)
                        try:
                            cmd = p.cmdline()
                            label = " ".join(cmd[:3]) if cmd else p.name()
                        except Exception:
                            label = p.name()
                        rows.append({
                            "pid":     p.pid,
                            "name":    p.name(),
                            "cmd":     label[:80],
                            "cpu_pct": round(cpu, 1),
                            "mem_mb":  mem,
                            "status":  p.status(),
                        })
                    except Exception:
                        pass
                rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
                return rows[:12]
            except ImportError:
                return [{"error": "psutil not available"}]
        procs = await asyncio.to_thread(_collect)
        return JSONResponse({"processes": procs})

    @app.get("/api/diagnostics/full")
    async def get_diagnostics_full() -> JSONResponse:
        nonlocal _full_diag_cache, _full_diag_cache_time
        now = time.monotonic()
        if _full_diag_cache is None or (now - _full_diag_cache_time) > _FULL_DIAG_TTL_S:
            _full_diag_cache = await asyncio.to_thread(
                gather_full_diagnostics,
                megapi,
                safety_monitor=safety_monitor,
                body_tracker=body_tracker,
                camera_health_url=camera_health_url,
                api_port=int(config.get("server", {}).get("port", 8082)),
            )
            _full_diag_cache_time = time.monotonic()
        return JSONResponse(_full_diag_cache)

    @app.post("/api/diagnostics/full/run")
    async def run_diagnostics_full() -> JSONResponse:
        nonlocal _full_diag_cache, _full_diag_cache_time
        try:
            report = await asyncio.wait_for(
                asyncio.to_thread(
                    gather_full_diagnostics,
                    megapi,
                    safety_monitor=safety_monitor,
                    body_tracker=body_tracker,
                    camera_health_url=camera_health_url,
                    api_port=int(config.get("server", {}).get("port", 8082)),
                ),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            report = dict(_full_diag_cache or {})
            report["timeout"] = True
        _full_diag_cache = report
        _full_diag_cache_time = time.monotonic()
        return JSONResponse(report)

    @app.get("/api/metrics/history")
    async def get_metrics_history(metric: str = "cpu_percent", minutes: int = 60) -> JSONResponse:
        since = int(time.time()) - minutes * 60
        data = await asyncio.to_thread(_mstore.query, metric, since)
        names = await asyncio.to_thread(_mstore.available)
        return JSONResponse({"metric": metric, "data": data, "available": names,
                             "db_size_mb": _mstore.db_size_mb()})

    @app.get("/api/alerts/current")
    async def get_alerts_current() -> JSONResponse:
        return JSONResponse({"alerts": _active_alerts, "count": len(_active_alerts)})

    # ── Audio routing ────────────────────────────────────────────────────────

    @app.post("/api/audio/route")
    async def audio_route(request: Request) -> JSONResponse:
        """Switch audio target: {"mode": "ROVER_A32"} or {"mode": "BUDS"}."""
        if audio_router is None:
            raise HTTPException(status_code=503, detail="audio_router not available")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        mode = str(body.get("mode", ""))
        if mode not in ("ROVER_A32", "BUDS"):
            return JSONResponse({"status": "error", "message": f"unknown mode: {mode}"}, status_code=400)
        await audio_router.set_mode(mode)
        ip, port = audio_router._target()
        return JSONResponse({"status": "ok", "mode": mode, "target": f"{ip}:{port}"})

    # ── Voice endpoints ──────────────────────────────────────────────────────

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(
        audio: UploadFile = File(...),
        lang: str = Form("en"),
    ) -> JSONResponse:
        """Multipart: field 'audio' (wav/webm) + optional 'lang'. Always returns JSON."""
        if not _VOICE:
            return JSONResponse({"transcript": "", "error": "voice_engine not available"})
        try:
            audio_bytes = await audio.read()
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _voice_engine.transcribe, audio_bytes, lang),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = {"transcript": "", "error": "timeout"}
        except Exception as exc:
            result = {"transcript": "", "error": str(exc)}
        return JSONResponse(result)

    @app.post("/api/voice/wake")
    async def voice_wake(
        audio: UploadFile = File(...),
        lang: str = Form("en"),
    ) -> JSONResponse:
        """Wake-word check — transcribe 2.5 s audio chunk, return {wake, transcript}.

        Called by A32 VAD pipeline; returns {wake: true} when 'rover' is heard.
        Uses faster-whisper tiny (int8, CPU) — no cloud, fully local.
        """
        if not _VOICE:
            return JSONResponse({"wake": False, "transcript": "", "error": "voice_engine not available"})
        try:
            audio_bytes = await audio.read()
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _voice_engine.transcribe_wake, audio_bytes, lang),
                timeout=10.0,  # beam_size=3 needs ~1-2s extra vs beam_size=1
            )
        except asyncio.TimeoutError:
            result = {"transcript": "", "error": "timeout"}
        except Exception as exc:
            result = {"transcript": "", "error": str(exc)}
        transcript = result.get("transcript", "").lower().strip()
        # Fuzzy match — accept common whisper mishearings of Czech-accented "rover"
        matched = next((w for w in _WAKE_WORDS if w in transcript), None)
        wake = matched is not None
        if wake:
            logger.info("voice/wake: TRIGGERED matched=%r transcript=%r", matched, transcript)
        else:
            logger.info("voice/wake: no match transcript=%r", transcript)
        return JSONResponse({"wake": wake, "transcript": transcript})

    @app.post("/api/voice/speak")
    async def voice_speak(request: Request) -> StreamingResponse:
        """JSON {"text": "...", "lang": "en"} → streaming WAV audio."""
        if not _VOICE:
            raise HTTPException(status_code=503, detail="voice_engine not available")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        text = str(body.get("text", ""))[:500]
        lang = str(body.get("lang", "en"))
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        loop = asyncio.get_event_loop()
        def _generate():
            return b"".join(_voice_engine.speak(text, lang))
        wav_bytes = await loop.run_in_executor(None, _generate)
        return StreamingResponse(
            iter([wav_bytes]),
            media_type="audio/wav",
            headers={"Content-Length": str(len(wav_bytes))},
        )

    @app.get("/api/faults")
    async def get_faults() -> JSONResponse:
        return JSONResponse({"faults": list(_fault_history), "count": len(_fault_history)})

    @app.delete("/api/faults")
    async def clear_faults() -> JSONResponse:
        _fault_history.clear()
        return JSONResponse({"status": "ok"})

    @app.get("/api/services")
    async def get_services() -> JSONResponse:
        """Live status of key services + thermal + ollama + camera + memory + disk. On-demand only."""
        import subprocess
        import shutil
        import resource as _resource

        _MONITORED = [
            "rover2-api.service",
            "rover-camera.service",
            "rover2-virtual-usb-dongle.service",
            "rover2-powerbank-keepalive.service",
            "hailo-ollama.service",
        ]

        def _check_services() -> dict:
            result = {}
            for unit in _MONITORED:
                try:
                    r = subprocess.run(
                        ["systemctl", "is-active", unit],
                        capture_output=True, text=True, timeout=3,
                    )
                    status = r.stdout.strip() or "unknown"
                    result[unit] = {"active": status == "active", "status": status}
                except Exception as exc:
                    result[unit] = {"active": False, "status": "error", "detail": str(exc)[:60]}
            return result

        async def _probe_ollama() -> dict:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get("http://localhost:11434/api/tags")
                return {"responsive": r.status_code == 200}
            except Exception as exc:
                return {"responsive": False, "detail": str(exc)[:60]}

        async def _probe_camera_stream() -> dict:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.head("http://localhost:8081/stream")
                return {"responsive": r.status_code < 500}
            except Exception as exc:
                return {"responsive": False, "detail": str(exc)[:60]}

        def _check_disk_and_memory() -> dict:
            result: dict = {}
            try:
                u = shutil.disk_usage("/")
                result["disk_root_pct"] = round(100 * u.used / u.total, 1)
                result["disk_root_free_gb"] = round(u.free / 1e9, 2)
            except Exception:
                pass
            try:
                u2 = shutil.disk_usage("/opt")
                result["disk_opt_pct"] = round(100 * u2.used / u2.total, 1)
            except Exception:
                pass
            try:
                import psutil as _psu
                result["memory_rss_mb"] = round(_psu.Process().memory_info().rss / (1024 ** 2), 1)
            except Exception:
                try:
                    ru = _resource.getrusage(_resource.RUSAGE_SELF)
                    result["memory_rss_mb"] = round(ru.ru_maxrss / 1024, 1)
                except Exception:
                    pass
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 4 and parts[1] == "/boot/firmware":
                            result["boot_partition_ro"] = "rw" not in parts[3].split(",")
                            break
            except Exception:
                pass
            return result

        services, thermal_data, ollama_probe, camera_probe, disk_mem = await asyncio.gather(
            asyncio.to_thread(_check_services),
            asyncio.to_thread(read_thermal) if _THERMAL else asyncio.sleep(0),
            _probe_ollama(),
            _probe_camera_stream(),
            asyncio.to_thread(_check_disk_and_memory),
        )
        freq_mhz = await asyncio.to_thread(read_cpu_freq_mhz) if _THERMAL else None
        if isinstance(thermal_data, dict):
            thermal_data["freq_mhz"] = freq_mhz
        else:
            thermal_data = {"freq_mhz": freq_mhz}

        nonlocal _thermal_alert, _ollama_unresponsive_count, _camera_restart_at, _camera_alert, _ollama_alert
        if thermal_monitor is not None and isinstance(thermal_data, dict):
            _thermal_alert = thermal_monitor.evaluate(thermal_data)
        else:
            _thermal_alert = None

        # Ollama consecutive unresponsive tracking
        ollama_svc_active = services.get("hailo-ollama.service", {}).get("active", False)
        if ollama_svc_active and not ollama_probe.get("responsive", False):
            _ollama_unresponsive_count += 1
            if _ollama_unresponsive_count >= 2:
                _ollama_alert = {
                    "metric": "ollama_responsive", "value": 0,
                    "severity": "warn", "ts": int(time.time()),
                    "msg": f"Ollama active but unresponsive ({_ollama_unresponsive_count} consecutive checks)",
                }
                _add_fault("ollama", f"Unresponsive x{_ollama_unresponsive_count}", "alert")
        else:
            _ollama_unresponsive_count = 0
            _ollama_alert = None

        # Camera stream health + auto-restart (5-min cooldown)
        if not camera_probe.get("responsive", False):
            now_m = time.monotonic()
            if now_m - _camera_restart_at >= 300.0:
                _camera_restart_at = now_m
                _add_fault("camera", "Camera stream unresponsive — restarting rover-camera.service", "auto-restart")
                subprocess.Popen(["sudo", "/usr/bin/systemctl", "restart", "rover-camera.service"])
            _camera_alert = {
                "metric": "camera_responsive", "value": 0,
                "severity": "warn", "ts": int(time.time()),
                "msg": "Camera stream unresponsive",
            }
        else:
            _camera_alert = None

        _rss = disk_mem.get("memory_rss_mb")
        if _rss is not None:
            if _rss > _MEM_EMERGENCY_MB:
                _mem_level = "emergency"
            elif _rss > _MEM_CRITICAL_MB:
                _mem_level = "critical"
            elif _rss > _MEM_WARN_MB:
                _mem_level = "warn"
            else:
                _mem_level = "ok"
        else:
            _mem_level = "ok"
        return JSONResponse({
            "services": services,
            "thermal": thermal_data,
            "ollama": {**ollama_probe, "unresponsive_count": _ollama_unresponsive_count},
            "camera_stream": camera_probe,
            "memory": {
                "rss_mb": _rss,
                "target_mb": _MEM_TARGET_MB,
                "warn_mb": _MEM_WARN_MB,
                "critical_mb": _MEM_CRITICAL_MB,
                "level": _mem_level,
            },
            "memory_rss_mb": _rss,
            "disk_root_pct": disk_mem.get("disk_root_pct"),
            "disk_root_free_gb": disk_mem.get("disk_root_free_gb"),
            "disk_opt_pct": disk_mem.get("disk_opt_pct"),
            "boot_partition_ro": disk_mem.get("boot_partition_ro"),
            "ws_client_count": hub.client_count,
        })

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Prometheus exposition format — for Grafana at 192.168.70.10."""
        since = int(time.time()) - 60
        expose = [
            "cpu_percent", "ram_percent", "ram_used_mb", "swap_percent",
            "disk_percent", "disk_read_bps", "disk_write_bps",
            "temp_cpu_thermal", "temp_rp1_adc",
            "net_eth0_tx_bps", "net_eth0_rx_bps",
            "net_wlan0_tx_bps", "net_wlan0_rx_bps",
            "process_cpu_percent", "process_memory_mb",
            "ultrasonic_cm", "ble_rssi", "ble_seen",
            "follow_enabled", "detect_only", "follow_state",
            "hailo_ready", "person_detected", "safety_blocked",
        ]
        latest = await asyncio.to_thread(_mstore.query_latest, expose, since)
        lines: list[str] = ["# rover2 metrics"]
        for name, val in latest.items():
            if val is None:
                continue
            prom = f"rover2_{name}"
            lines.append(f"# TYPE {prom} gauge")
            lines.append(f"{prom} {val}")
        lines.append("")
        return Response("\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/api/maintenance/wifi-restore")
    async def maintenance_wifi_restore() -> JSONResponse:
        """Re-apply WiFi from /boot/firmware/network-config (needs passwordless sudo)."""
        result = await asyncio.to_thread(_wifi_restore_privileged)
        return JSONResponse(result)

    @app.post("/api/maintenance/restart-service")
    async def maintenance_restart_service() -> JSONResponse:
        """Restart rover2-api.service (non-blocking — connection will drop briefly)."""
        import subprocess
        subprocess.Popen(["sudo", "systemctl", "restart", "--no-block", "rover2-api.service"])
        return JSONResponse({"status": "ok", "detail": "restarting rover2-api.service"})

    @app.post("/api/maintenance/restart-pi")
    async def maintenance_restart_pi() -> JSONResponse:
        """Reboot the Pi (non-blocking)."""
        import subprocess
        subprocess.Popen(["sudo", "/sbin/reboot"])
        return JSONResponse({"status": "ok", "detail": "Pi rebooting"})

    # ── Auto-stop task for chat movement commands ────────────────────────────
    _auto_stop_task: asyncio.Task | None = None

    async def _timed_stop(delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            megapi.stop_motors()
        except Exception:
            pass

    def _schedule_auto_stop(delay: float = 2.0) -> None:
        nonlocal _auto_stop_task
        if _auto_stop_task is not None and not _auto_stop_task.done():
            _auto_stop_task.cancel()
        _auto_stop_task = asyncio.create_task(_timed_stop(delay))

    @app.post("/api/chat")
    async def chat(request: Request) -> JSONResponse:
        """Natural language command: {message: str} → {reply, action, executed}."""
        from chat_router import route as _route

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")

        r = _route(message)
        action = r["action"]
        reply: str | None = r["reply"]
        executed = False

        try:
            if action == "stop":
                megapi.stop_motors()
                executed = True
            elif action in ("forward", "backward", "left", "right"):
                dir_key = "back" if action == "backward" else action
                mult_l, mult_r = directions.get(dir_key, (0, 0))
                megapi.drive(mult_l * default_speed, mult_r * default_speed)
                _schedule_auto_stop(2.0)
                executed = True
            elif action == "follow":
                if body_tracker is not None and body_tracker.available:
                    body_tracker.set_enabled(True)
                    executed = True
                else:
                    reply = "Follow not available (Hailo not ready)."
            elif action == "unfollow":
                if body_tracker is not None:
                    body_tracker.set_enabled(False)
                    body_tracker.set_detect_only(False)
                    megapi.stop_motors()
                    executed = True
            elif action == "detect":
                if body_tracker is not None and body_tracker.available:
                    body_tracker.set_detect_only(True)
                    executed = True
                else:
                    reply = "Detection not available."
            elif action == "undetect":
                if body_tracker is not None:
                    body_tracker.set_detect_only(False)
                    executed = True
            elif action == "grip_open":
                megapi.grip("open")
                executed = True
            elif action == "grip_close":
                megapi.grip("close")
                executed = True
            elif action == "arm_up":
                megapi.arm(action="up")
                executed = True
            elif action == "arm_down":
                megapi.arm(action="down")
                executed = True
            elif action == "describe":
                if vlm_engine is None:
                    reply = "VLM not available."
                else:
                    frame = await asyncio.to_thread(vlm_engine.fetch_camera_frame)
                    if frame is None:
                        reply = "Camera unavailable."
                    else:
                        desc = await vlm_engine.describe(
                            frame, "Describe briefly what you see in front of the robot."
                        )
                        reply = desc or "Could not generate description."
                executed = bool(reply)
        except RuntimeError as exc:
            reply = f"Error: {exc}"

        return JSONResponse({"reply": reply, "action": action, "executed": executed})

    @app.get("/api/vision/describe")
    async def vision_describe(prompt: str = "Describe briefly what you see.") -> JSONResponse:
        """Snapshot → VLM → description text. Lazy-loads VLM on first call."""
        if vlm_engine is None:
            raise HTTPException(status_code=503, detail="VLM not configured")
        frame = await asyncio.to_thread(vlm_engine.fetch_camera_frame)
        if frame is None:
            raise HTTPException(status_code=503, detail="Camera unavailable")
        desc = await vlm_engine.describe(frame, prompt)
        return JSONResponse({
            "description": desc,
            "vlm_status": vlm_engine.get_status(),
        })

    @app.get("/api/chat/status")
    async def chat_status() -> JSONResponse:
        agent_status = rover_agent.get_status() if rover_agent is not None else {"available": None}
        return JSONResponse({
            "vlm": vlm_engine.get_status() if vlm_engine is not None else {"status": "skip", "detail": "not configured"},
            "agent": agent_status,
        })

    @app.post("/api/agent/chat")
    async def agent_chat(request: Request) -> JSONResponse:
        """Agentic chat: model calls tools autonomously, proposes dangerous actions.

        Body: {messages: [{role, content}, ...]}
        Spoken path: {message: "text", spoken: true, lang: "en"} or legacy {messages: [...]}
        Returns: {reply, tool_log, action_proposal}
        """
        if rover_agent is None:
            raise HTTPException(status_code=503, detail="Agent not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc

        # Spoken voice path — agentic tool-use loop with ROVER personality
        if body.get("spoken"):
            # Accept simple {message: "..."} or legacy {messages: [{role, content}]}
            user_text = ""
            if body.get("message"):
                user_text = str(body.get("message", "")).strip()
            else:
                messages = body.get("messages", [])
                user_text = next(
                    (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
                )
            if not user_text:
                return JSONResponse({"reply": "", "tool_log": [], "action_proposal": None})
            lang = str(body.get("lang", "en"))
            try:
                reply_tuple = await asyncio.wait_for(
                    rover_agent.run_spoken_turn(user_text, lang),
                    timeout=95.0,
                )
            except asyncio.TimeoutError:
                reply_tuple = ("", "agent")
            reply = reply_tuple[0] if isinstance(reply_tuple, tuple) else reply_tuple
            logger.info("Agent: spoken turn OK (%d chars) via agent — TTS on A32", len(reply))
            return JSONResponse({"reply": reply, "tool_log": [], "action_proposal": None})

        messages = body.get("messages", [])
        if not messages:
            raise HTTPException(status_code=400, detail="messages required")

        try:
            turn = await asyncio.wait_for(rover_agent.run_turn(messages), timeout=240.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="Agent timed out (240s). First Ollama reply can take 1–2 min.",
            ) from None
        return JSONResponse({
            "reply":           turn.reply,
            "tool_log":        turn.tool_log,
            "action_proposal": turn.action_proposal,
        })

    @app.post("/api/voice/converse")
    async def voice_converse(request: Request) -> JSONResponse:
        """Voice conversation turn for wake-word assistant.

        Body: {message, history, spoken, lang, session_id}
        Response: {reply, routed_to, session_id}
        """
        nonlocal _conversation_active, _tracking_was_enabled, _conversation_timeout_task
        if rover_agent is None:
            raise HTTPException(status_code=503, detail="Agent not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc

        message    = str(body.get("message", "")).strip()
        history    = body.get("history", [])
        lang       = str(body.get("lang", "en"))
        session_id = str(body.get("session_id", ""))
        if not message:
            raise HTTPException(status_code=400, detail="message required")

        # Pause FOLLOW on first call in this conversation
        if not _conversation_active:
            _conversation_active = True
            if _VOICE:
                _voice_engine.set_active(True)
            if body_tracker is not None and body_tracker.enabled:
                _tracking_was_enabled = True
                body_tracker.set_enabled(False)
                logger.info("voice: FOLLOW paused for conversation session=%s", session_id[:8])
            else:
                _tracking_was_enabled = False

        # Server-side 60s auto-end (cancel previous if any)
        if _conversation_timeout_task is not None and not _conversation_timeout_task.done():
            _conversation_timeout_task.cancel()

        async def _auto_end() -> None:
            nonlocal _conversation_active, _tracking_was_enabled
            await asyncio.sleep(60.0)
            if _conversation_active:
                _conversation_active = False
                if _VOICE:
                    _voice_engine.set_active(False)
                if _tracking_was_enabled and body_tracker is not None:
                    body_tracker.set_enabled(True)
                    _tracking_was_enabled = False
                    logger.info("voice: FOLLOW restored after 60s timeout session=%s", session_id[:8])

        _conversation_timeout_task = asyncio.ensure_future(_auto_end())

        try:
            reply_tuple = await asyncio.wait_for(
                rover_agent.run_spoken_turn(message, lang, history),
                timeout=95.0,
            )
        except asyncio.TimeoutError:
            reply_tuple = ("", "agent")

        reply     = reply_tuple[0] if isinstance(reply_tuple, tuple) else reply_tuple
        routed_to = reply_tuple[1] if isinstance(reply_tuple, tuple) else "agent"

        # Detect social closing in user message — signal A32 to end session after TTS.
        end_session = bool(_CLOSING_PATTERNS.search(message))
        if end_session:
            logger.info("voice: closing phrase detected, ending session=%s", session_id[:8])
            if _conversation_timeout_task is not None and not _conversation_timeout_task.done():
                _conversation_timeout_task.cancel()
            _conversation_active = False
            if _VOICE:
                _voice_engine.set_active(False)
            if _tracking_was_enabled and body_tracker is not None:
                body_tracker.set_enabled(True)
                _tracking_was_enabled = False
                logger.info("voice: FOLLOW restored after closing phrase session=%s", session_id[:8])

        logger.info(
            "Agent: spoken turn OK (%d chars) via %s — TTS on A32%s",
            len(reply), routed_to, " [end_session]" if end_session else "",
        )
        return JSONResponse({
            "reply":       reply,
            "routed_to":   routed_to,
            "session_id":  session_id,
            "end_session": end_session,
        })

    @app.post("/api/voice/converse/end")
    async def voice_converse_end(request: Request) -> JSONResponse:
        """Signal end of voice conversation — restores FOLLOW if it was paused."""
        nonlocal _conversation_active, _tracking_was_enabled, _conversation_timeout_task
        try:
            body = await request.json()
        except Exception:
            body = {}
        session_id = str(body.get("session_id", ""))

        if _conversation_timeout_task is not None and not _conversation_timeout_task.done():
            _conversation_timeout_task.cancel()

        _conversation_active = False
        if _VOICE:
            _voice_engine.set_active(False)
        if _tracking_was_enabled and body_tracker is not None:
            body_tracker.set_enabled(True)
            _tracking_was_enabled = False
            logger.info("voice: FOLLOW restored after conversation end session=%s", session_id[:8])

        return JSONResponse({"status": "ok", "session_id": session_id})

    @app.post("/api/agent/confirm")
    async def agent_confirm(request: Request) -> JSONResponse:
        """Execute a dangerous action after explicit user confirmation.

        Body: {name: str, args: {}}
        """
        if rover_agent is None:
            raise HTTPException(status_code=503, detail="Agent not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        name = str(body.get("name", ""))
        args = dict(body.get("args", {}))
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        result = await rover_agent.execute_action(name, args)
        return JSONResponse({"status": "ok", "result": result})

    @app.get("/api/agent/status")
    async def agent_status() -> JSONResponse:
        if rover_agent is None:
            return JSONResponse({"available": False, "detail": "not configured"})
        avail = await rover_agent.check_available()
        return JSONResponse({**rover_agent.get_status(), "available": avail})

    @app.get("/api/agent/stats")
    async def agent_stats() -> JSONResponse:
        if rover_agent is None:
            return JSONResponse({"detail": "agent not configured"}, status_code=503)
        stats = rover_agent.get_agent_stats()
        if vlm_engine is not None:
            stats["vlm_cooldown_remaining_s"] = round(vlm_engine.cooldown_remaining(), 1)
            stats["vlm_last_describe_wait_s"] = round(vlm_engine._last_describe_wait_s, 1)
        return JSONResponse(stats)

    # ── Agent diagnostic tools endpoints ─────────────────────────────────────

    _ALLOWED_SERVICES = frozenset({
        "rover2-api", "rover-camera", "ollama",
        "rover2-powerbank-keepalive", "rover2-virtual-usb-dongle",
        "rover2-restore-wifi", "cpu-governor",
        "hailo-ollama", "bluetooth", "NetworkManager",
    })

    @app.get("/api/diagnostics/journal")
    async def get_journal(service: str = "rover2-api", lines: int = 40) -> JSONResponse:
        import subprocess
        lines = min(lines, 120)
        if service not in _ALLOWED_SERVICES:
            raise HTTPException(status_code=400, detail=f"Service not in allowed list")
        def _run():
            r = subprocess.run(
                ["journalctl", "-u", service, f"-n{lines}", "--no-pager", "--output=short"],
                capture_output=True, text=True, timeout=8,
            )
            return r.stdout.splitlines()
        entries = await asyncio.to_thread(_run)
        return JSONResponse({"service": service, "lines": entries, "count": len(entries)})

    @app.get("/api/diagnostics/services")
    async def list_services() -> JSONResponse:
        import subprocess
        services = [
            "rover2-api", "rover-camera", "ollama",
            "rover2-powerbank-keepalive", "rover2-virtual-usb-dongle",
            "rover2-restore-wifi", "cpu-governor",
            "hailo-ollama", "bluetooth", "NetworkManager", "ssh",
        ]
        def _check():
            result = {}
            for svc in services:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=3,
                )
                active = r.stdout.strip()
                r2 = subprocess.run(
                    ["systemctl", "is-enabled", svc],
                    capture_output=True, text=True, timeout=3,
                )
                enabled = r2.stdout.strip()
                result[svc] = {"active": active, "enabled": enabled}
            return result
        statuses = await asyncio.to_thread(_check)
        return JSONResponse({"services": statuses})

    @app.post("/api/maintenance/service")
    async def service_action(request: Request) -> JSONResponse:
        """Start or stop an allowed service (requires sudoers entry)."""
        import subprocess
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        action  = str(body.get("action", "")).lower()
        service = str(body.get("service", ""))
        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=400, detail="action must be start/stop/restart")
        if service not in _ALLOWED_SERVICES:
            raise HTTPException(status_code=400, detail=f"Service not in allowed list: {sorted(_ALLOWED_SERVICES)}")
        flags = ["--no-block"] if action == "restart" else []
        subprocess.Popen(["sudo", "systemctl", action] + flags + [f"{service}.service"])
        return JSONResponse({"status": "ok", "action": action, "service": service})

    @app.get("/api/diagnostics/wifi")
    async def get_wifi_info() -> JSONResponse:
        import subprocess
        def _run():
            info: dict = {}
            # IP addresses
            r = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True, timeout=4)
            info["ip_addrs"] = r.stdout
            # WiFi details via iw
            r = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, timeout=4)
            info["wlan0_link"] = r.stdout.strip() or "wlan0 not connected"
            # Signal quality from /proc
            try:
                with open("/proc/net/wireless") as f:
                    info["wireless_proc"] = f.read().strip()
            except OSError:
                pass
            # nmcli for human-readable connection
            r = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,BARS,SECURITY", "device", "wifi"],
                capture_output=True, text=True, timeout=4,
            )
            info["nmcli_wifi"] = r.stdout.strip()
            # Default route / gateway
            r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3)
            info["default_route"] = r.stdout.strip()
            return info
        data = await asyncio.to_thread(_run)
        return JSONResponse(data)

    @app.get("/api/diagnostics/disk")
    async def get_disk_details() -> JSONResponse:
        def _run():
            import subprocess, shutil
            result: dict = {}
            try:
                import psutil
                for part in psutil.disk_partitions():
                    try:
                        u = psutil.disk_usage(part.mountpoint)
                        result[part.mountpoint] = {
                            "total_gb": round(u.total / 1e9, 1),
                            "used_gb":  round(u.used  / 1e9, 1),
                            "free_gb":  round(u.free  / 1e9, 1),
                            "pct":      u.percent,
                            "fstype":   part.fstype,
                        }
                    except Exception:
                        pass
            except ImportError:
                pass
            # Key directories
            dirs = ["/opt/rover2", "/opt/rover", "/var/log", "/tmp"]
            for d in dirs:
                r = subprocess.run(["du", "-sh", d], capture_output=True, text=True, timeout=5)
                result[f"du:{d}"] = r.stdout.strip()
            return result
        data = await asyncio.to_thread(_run)
        return JSONResponse(data)

    @app.get("/api/diagnostics/wifi/ping")
    async def ping_host(host: str = "192.168.70.1") -> JSONResponse:
        import subprocess
        def _run():
            r = subprocess.run(
                ["ping", "-c", "3", "-W", "2", host],
                capture_output=True, text=True, timeout=12,
            )
            lines = r.stdout.splitlines()
            stats = next((l for l in reversed(lines) if "packets" in l), "")
            rtt   = next((l for l in reversed(lines) if "rtt" in l or "round-trip" in l), "")
            return {"host": host, "reachable": r.returncode == 0,
                    "stats": stats, "rtt": rtt, "raw": r.stdout[-400:]}
        data = await asyncio.to_thread(_run)
        return JSONResponse(data)

    @app.get("/api/diagnostics/hailo")
    async def check_hailo() -> JSONResponse:
        def _run():
            info: dict = {"hef_yolo": "/opt/rover/models/yolov8m_h10.hef",
                          "hef_vlm":  "/opt/rover/models/Qwen2-VL-2B-Instruct.hef"}
            from pathlib import Path
            info["yolo_present"] = Path(info["hef_yolo"]).exists()
            info["vlm_present"]  = Path(info["hef_vlm"]).exists()
            try:
                import hailo_platform  # type: ignore[import]
                info["hailo_platform_version"] = getattr(hailo_platform, "__version__", "installed")
                info["hailo_available"] = True
            except ImportError as exc:
                info["hailo_available"] = False
                info["error"] = str(exc)
            if body_tracker is not None:
                st = body_tracker.get_state()
                info["body_tracker_ready"]  = st.get("hailo_ready", False)
                info["body_tracker_running"] = st.get("running", False)
            if vlm_engine is not None:
                info["vlm_status"] = vlm_engine.get_status()
            return info
        data = await asyncio.to_thread(_run)
        return JSONResponse(data)

    @app.get("/api/diagnostics/db")
    async def get_db_stats() -> JSONResponse:
        names  = await asyncio.to_thread(_mstore.available)
        sample = await asyncio.to_thread(_mstore.query_latest, names[:8], int(time.time()) - 300)
        return JSONResponse({
            "db_size_mb":    _mstore.db_size_mb(),
            "metric_count":  len(names),
            "metrics":       names,
            "latest_sample": sample,
        })

    @app.get("/api/robot/capabilities")
    async def robot_capabilities() -> JSONResponse:
        """Self-describing capability list — always reflects actual server endpoints."""
        return JSONResponse({
            "version": "rover2-2.0",
            "drive":      ["forward", "back", "left", "right", "stop"],
            "arm":        ["up", "down", "pulse-up", "pulse-down"],
            "gripper":    ["open", "close"],
            "follow":     ["camera-YOLO", "BLE-beacon-fallback"],
            "detection":  ["YOLOv8m-Hailo-10H", "detect-only-mode"],
            "vlm":        ["Qwen2-VL-2B-Instruct", "scene-description"],
            "agent":      ["Ollama-local", "tool-use", "auto-diagnose", "auto-fix"],
            "safety":     ["ultrasonic-forward-block", "heartbeat-timeout-stop"],
            "ble":        ["Samsung-Flip6-beacon", "RSSI-tracking"],
            "monitoring": ["SQLite-metrics", "Prometheus-endpoint", "Chart.js-graphs"],
            "endpoints": [
                rule.path for rule in app.routes
                if hasattr(rule, "path") and rule.path.startswith("/api")
            ],
        })

    @app.get("/api/logs")
    async def get_logs() -> JSONResponse:
        """Last ~200 in-process log records (same process as rover2-api)."""
        return JSONResponse({"records": get_log_handler().get_records()})

    @app.delete("/api/logs")
    async def clear_logs() -> JSONResponse:
        get_log_handler().clear()
        return JSONResponse({"status": "ok"})

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        """Read-only config.yaml snapshot (secrets masked)."""
        return JSONResponse({
            "path": str(config_path),
            "config": public_config(config),
            "tuning_schema": tuning_schema_public(),
        })

    @app.get("/api/config/schema")
    async def get_config_schema() -> JSONResponse:
        """All tunable parameters with types and allowed ranges."""
        return JSONResponse({"sections": tuning_schema_public()})

    @app.post("/api/config/tuning")
    async def post_config_tuning(request: Request) -> JSONResponse:
        """Apply tuning (runtime + config.yaml). Body: {section: {key: value, ...}, ...}."""
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        try:
            patch = extract_tuning_patch(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        nonlocal config, default_speed, max_speed
        merged = merge_tuning(config, patch)
        applied = apply_config_patch(
            patch,
            config=merged,
            megapi=megapi,
            safety_monitor=safety_monitor,
            body_tracker=body_tracker,
            hub=hub,
            rover_agent=rover_agent,
        )
        if "drive" in patch:
            if "default_speed" in patch["drive"]:
                default_speed = int(patch["drive"]["default_speed"])
            if "max_speed" in patch["drive"]:
                max_speed = int(patch["drive"]["max_speed"])
        try:
            await asyncio.to_thread(save_config, config_path, merged)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not save config: {exc}") from exc
        config.clear()
        config.update(merged)
        return JSONResponse({"status": "ok", "applied": applied, "config": public_config(config)})

    @app.post("/api/camera/wake")
    async def camera_wake() -> JSONResponse:
        """Manually wake camera from idle sleep."""
        if cam_idle is not None:
            cam_idle.wake("manual API call")
        return JSONResponse({"status": "ok", "camera_sleeping": cam_idle.sleeping if cam_idle is not None else False})

    @app.get("/api/status")
    async def status() -> JSONResponse:
        payload = build_telemetry(
            megapi, safety_monitor, _START, _public_ultrasonic_cm, body_tracker, cam_idle
        )
        payload.pop("type", None)
        payload["ws_client_count"] = hub.client_count
        payload["audio_mode"] = audio_router.get_mode() if audio_router else None
        payload["ha_available"] = rover_agent.ha_available if rover_agent is not None else False
        if watchdog is not None:
            payload["watchdog_actions"] = watchdog.actions_used
            payload["watchdog_ok"]      = watchdog.ok
        return JSONResponse(payload)

    @app.get("/api/watchdog/log")
    async def watchdog_log() -> JSONResponse:
        """Return last 50 lines of watchdog.log."""
        from watchdog import _LOG_PATH as _WD_LOG
        try:
            if _WD_LOG.exists():
                lines = _WD_LOG.read_text(errors="replace").splitlines()
                return JSONResponse({"lines": lines[-50:], "total_lines": len(lines)})
        except Exception as exc:
            return JSONResponse({"lines": [], "error": str(exc)})
        return JSONResponse({"lines": [], "total_lines": 0})

    @app.post("/api/watchdog/reset")
    async def watchdog_reset() -> JSONResponse:
        """Reset the watchdog action counter and limit-reached flag."""
        if watchdog is None:
            raise HTTPException(status_code=503, detail="watchdog not running")
        watchdog.reset_actions()
        return JSONResponse({"status": "ok", "actions_used": watchdog.actions_used})

    @app.get("/api/watchdog/status")
    async def watchdog_status_detail() -> JSONResponse:
        """Detailed watchdog state — last cycle, last action, persistent alerts."""
        if watchdog is None:
            return JSONResponse({
                "status": "disabled", "actions_used": 0, "max_actions": 3,
                "last_cycle": "", "last_action": "", "last_action_ts": 0,
                "persistent_alerts": [],
            })
        return JSONResponse({
            "status": "ok" if watchdog.ok else "limit_reached",
            "actions_used": watchdog.actions_used,
            "max_actions": watchdog._max_actions,
            "last_cycle": watchdog.last_cycle_iso,
            "last_action": watchdog.last_action,
            "last_action_ts": watchdog.last_action_ts,
            "persistent_alerts": watchdog.get_persistent_alerts(),
        })

    @app.get("/api/network/status")
    async def network_status() -> JSONResponse:
        """AP state, WLAN1 channel, rtw88_8812au driver, wlan0 connectivity."""
        results: dict[str, Any] = {
            "wlan0_connected": False, "wlan1_ap_active": False,
            "wlan1_channel": None, "rtw88_8812au_loaded": False,
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "nmcli", "-t", "-f", "DEVICE,STATE", "device",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            results["wlan0_connected"] = b"wlan0:connected" in out
        except Exception:
            pass
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "iw", "dev", "wlan1", "info",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            text = out.decode(errors="replace")
            results["wlan1_ap_active"] = "type AP" in text
            for line in text.splitlines():
                if "channel" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            results["wlan1_channel"] = int(parts[1])
                        except ValueError:
                            pass
                    break
        except Exception:
            pass
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsmod",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            results["rtw88_8812au_loaded"] = b"rtw88_8812au" in out
        except Exception:
            pass
        return JSONResponse(results)

    @app.post("/api/watchdog/test-alert")
    async def watchdog_test_alert() -> JSONResponse:
        """Broadcast a test alert via WebSocket telemetry_extra (dev/debug only)."""
        test_alert = {
            "metric": "watchdog_test", "value": 1,
            "severity": "warn", "ts": int(time.time()),
            "msg": "TEST — watchdog alert pipeline OK",
            "source": "watchdog",
        }
        extra = dict(hub._telemetry_extra)
        existing = list(extra.get("alerts", []))
        existing.append(test_alert)
        extra["alerts"] = existing
        hub.set_telemetry_extra(extra)
        return JSONResponse({"status": "ok", "alert": test_alert})

    @app.post("/api/tracking")
    async def tracking(request: Request) -> JSONResponse:
        if body_tracker is None:
            raise HTTPException(status_code=503, detail="tracking not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        enabled = bool(body.get("enabled", False))
        detect_only = bool(body.get("detect_only", False))
        follow_mode = body.get("follow_mode")  # "fused" | "camera" | "ble" | None

        # follow_mode is a higher-level shorthand — apply first, then legacy flags
        if follow_mode is not None:
            if follow_mode not in ("fused", "camera", "ble"):
                raise HTTPException(status_code=400, detail="follow_mode must be 'fused', 'camera', or 'ble'")
            if not body_tracker.available and follow_mode != "ble":
                raise HTTPException(status_code=503, detail="Hailo not available")
            if cam_idle is not None:
                cam_idle.notify_tracking_active()
            body_tracker.set_follow_mode(follow_mode)
            # set_follow_mode already sets enabled=True for "ble" mode; mirror for camera/fused
            if follow_mode in ("camera", "fused"):
                body_tracker.set_enabled(True)
        else:
            if (enabled or detect_only) and not body_tracker.available:
                raise HTTPException(status_code=503, detail="Hailo not available")
            if "ble_follow_enabled" in body:
                body_tracker.set_ble_follow_enabled(bool(body["ble_follow_enabled"]))
            if detect_only:
                if cam_idle is not None:
                    cam_idle.notify_tracking_active()
                body_tracker.set_detect_only(True)
            elif enabled:
                if cam_idle is not None:
                    cam_idle.notify_tracking_active()
                body_tracker.set_enabled(True)
            else:
                body_tracker.set_enabled(False)
                body_tracker.set_detect_only(False)
                try:
                    megapi.stop_motors()
                except RuntimeError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({
            "status": "ok",
            "enabled": body_tracker.enabled,
            "detect_only": body_tracker.detect_only,
            "follow_mode": body_tracker._follow_mode,
            "ble_follow_enabled": body_tracker._ble_follow_enabled,
        })

    @app.get("/api/guard/status")
    async def guard_status() -> JSONResponse:
        if guard_controller is None:
            return JSONResponse({"guard_enabled": False, "state": "DISARMED",
                                 "beacon_rssi": None, "armed_since": None,
                                 "detections_this_session": 0, "last_detection": None,
                                 "last_webhook_ok": None, "last_webhook_at": None})
        return JSONResponse(guard_controller.get_stats())

    @app.post("/api/guard/override")
    async def guard_override(request: Request) -> JSONResponse:
        if guard_controller is None:
            raise HTTPException(status_code=503, detail="guard not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        action = str(body.get("action", "")).lower()
        if action not in ("arm", "disarm", "enable", "disable"):
            raise HTTPException(status_code=400, detail="action must be arm|disarm|enable|disable")
        new_state = await guard_controller.manual_override(action)
        return JSONResponse({"status": "ok", "state": new_state})

    @app.post("/api/drive")
    async def drive(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        direction = str(body.get("direction", "stop")).lower()
        speed_frac = float(body.get("speed", 1.0))
        if direction not in directions:
            raise HTTPException(status_code=400, detail=f"Unknown direction: {direction}")
        mult_l, mult_r = directions[direction]
        speed = _scale(speed_frac, max_speed if direction != "stop" else 0)
        left = mult_l * speed
        right = mult_r * speed
        try:
            if direction == "stop":
                megapi.stop_motors()
            else:
                megapi.drive(left, right)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "direction": direction, "l": left, "r": right})

    @app.post("/api/stop")
    async def stop() -> JSONResponse:
        try:
            megapi.stop_motors()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok"})

    @app.post("/api/grip")
    async def grip(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        action = str(body.get("action", "")).lower()
        if action not in ("open", "close"):
            raise HTTPException(status_code=400, detail="action must be open or close")
        try:
            megapi.grip(action)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "action": action})

    @app.post("/api/arm")
    async def arm(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        pulse_action = str(body.get("action", "")).lower()
        if pulse_action in ("up", "down") and "direction" not in body:
            try:
                megapi.arm(action=pulse_action)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return JSONResponse({"status": "ok", "mode": "pulse", "action": pulse_action})
        direction = str(body.get("direction", "stop")).lower()
        speed_frac = float(body.get("speed", 1.0))
        try:
            pwm = arm_pwm(direction, speed_frac, config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            megapi.arm(pwm)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "mode": "speed", "direction": direction, "pwm": pwm})

    @app.post("/api/arm/wave")
    async def arm_wave() -> JSONResponse:
        """Wave arm greeting: up → down → up → down, total ≤2 s."""
        try:
            # up 600 ms
            megapi.arm(action="up")
            await asyncio.sleep(0.6)
            # down 400 ms
            megapi.arm(action="down")
            await asyncio.sleep(0.4)
            # up 400 ms
            megapi.arm(action="up")
            await asyncio.sleep(0.4)
            # down 400 ms
            megapi.arm(action="down")
            await asyncio.sleep(0.4)
            # stop
            megapi.arm(0)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "action": "wave_arm"})

    @app.get("/api/ultrasonic")
    async def ultrasonic() -> JSONResponse:
        try:
            megapi.request_ultrasonic(wait_s=0.6)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({
            "status": "ok",
            "ultrasonic_cm": _public_ultrasonic_cm(
                megapi.last_ultrasonic_cm, age_s=megapi.last_ultrasonic_age_s
            ),
            "ultrasonic_age_s": megapi.last_ultrasonic_age_s,
        })

    @app.get("/api/firmware/tools")
    async def firmware_tools() -> JSONResponse:
        from firmware_flash import tools_available

        ok, detail = tools_available()
        return JSONResponse({"available": ok, "detail": detail})

    @app.post("/api/firmware/upload")
    async def firmware_upload(request: Request) -> JSONResponse:
        """Save compiled .hex to /tmp/rover2_firmware.hex (multipart or raw body)."""
        from firmware_flash import DEFAULT_HEX

        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("firmware") or form.get("file")
            if upload is None:
                raise HTTPException(status_code=400, detail="Missing firmware file")
            data = await upload.read()  # type: ignore[union-attr]
        else:
            data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="Empty firmware payload")
        DEFAULT_HEX.write_bytes(data)
        return JSONResponse({"status": "ok", "path": str(DEFAULT_HEX), "bytes": len(data)})

    @app.post("/api/firmware/flash")
    async def firmware_flash() -> JSONResponse:
        """Flash /tmp/rover2_firmware.hex via avrdude (background job restarts rover2-api)."""
        from firmware_flash import flash_firmware_async

        try:
            result = flash_firmware_async()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(result)

    return app
