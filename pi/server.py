"""ROVER2 FastAPI server — motors, gripper, ultrasonic, static D-pad UI."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket
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

logger = logging.getLogger(__name__)
_START = time.monotonic()
_STATIC = Path(__file__).parent / "web" / "static"

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
    )

    app = FastAPI(title="ROVER2", version="2.0.0")
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    config_path = Path(__file__).parent / "config.yaml"

    _diag_cache: dict[str, Any] | None = None
    _diag_cache_time: float = 0.0
    _DIAG_TTL_S = 30.0
    _full_diag_cache: dict[str, Any] | None = None
    _full_diag_cache_time: float = 0.0
    _FULL_DIAG_TTL_S = 10.0

    _metrics_cfg = config.get("metrics", {})
    _METRICS_INTERVAL_S = float(_metrics_cfg.get("interval_s", 5.0))
    _METRICS_INTERVAL_HOT_S = float(_metrics_cfg.get("interval_hot_s", 15.0))
    _METRICS_CPU_HOT_PCT = float(_metrics_cfg.get("cpu_hot_threshold", 70.0))
    _METRICS_RETENTION_DAYS = int(_metrics_cfg.get("retention_days", 7))
    _PURGE_INTERVAL = max(1, int(3600 / _METRICS_INTERVAL_S))  # purge ~once/hour

    # Alert state — updated each metrics tick, pushed via WebSocket extra
    _active_alerts: list[dict] = []
    _cpu_high_since: float | None = None

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
                    # follow_state: 0=off 1=detect 2=camera 3=ble
                    if st.get("enabled"):
                        points["follow_state"] = 3.0 if st.get("ble_active") else 2.0
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

                await asyncio.to_thread(_mstore.write, points)

                # ── Threshold alert evaluation ──────────────────────────────
                nonlocal _active_alerts, _cpu_high_since
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
                # Ease Pi thermals: sample less often when CPU is already high
                if cpu_v is not None and cpu_v >= _METRICS_CPU_HOT_PCT:
                    _sleep_s = _METRICS_INTERVAL_HOT_S
                else:
                    _sleep_s = _METRICS_INTERVAL_S
                _active_alerts = new_alerts
                hub.set_telemetry_extra({"alerts": new_alerts})
                # ───────────────────────────────────────────────────────────

                purge_tick += 1
                if purge_tick >= _PURGE_INTERVAL:
                    await asyncio.to_thread(_mstore.purge, _METRICS_RETENTION_DAYS)
                    purge_tick = 0
            except Exception as exc:
                logger.debug("metrics_loop error: %s", exc)

    @app.on_event("startup")
    async def _start_metrics() -> None:
        asyncio.create_task(_metrics_loop())
        if rover_agent is not None:
            asyncio.create_task(rover_agent.warmup())

    @app.websocket("/ws")
    async def websocket_control(websocket: WebSocket) -> None:
        await websocket.accept()
        await hub.serve(websocket)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            _STATIC / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    if _CAMERA_PROXY:

        @app.get("/stream")
        async def camera_stream() -> StreamingResponse:
            """MJPEG proxy — same feed as rover-camera on port 8081."""
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
        Returns: {reply, tool_log, action_proposal}
        """
        if rover_agent is None:
            raise HTTPException(status_code=503, detail="Agent not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
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

    @app.get("/api/status")
    async def status() -> JSONResponse:
        payload = build_telemetry(
            megapi, safety_monitor, _START, _public_ultrasonic_cm, body_tracker
        )
        payload.pop("type", None)
        return JSONResponse(payload)

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
        if (enabled or detect_only) and not body_tracker.available:
            raise HTTPException(status_code=503, detail="Hailo not available")
        if "ble_follow_enabled" in body:
            body_tracker.set_ble_follow_enabled(bool(body["ble_follow_enabled"]))
        if detect_only:
            body_tracker.set_detect_only(True)
        elif enabled:
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
            "ble_follow_enabled": body_tracker._ble_follow_enabled,
        })

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
