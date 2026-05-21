"""ROVER2 diagnostics and maintenance script catalog (read-only checks)."""

from __future__ import annotations

import datetime
import glob
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from body_tracker import BodyTracker
    from megapi import MegaPiBridge
    from safety import SafetyMonitor


# Shown in web UI — dev scripts are copy-paste only; Pi actions use safe API routes.
SCRIPT_CATALOG: list[dict[str, Any]] = [
    {
        "id": "run_local_tests",
        "title": "Unit tests (dev PC)",
        "where": "dev",
        "command": "./scripts/run_local_tests.sh",
        "detail": "Run before deploy. No Pi required.",
    },
    {
        "id": "check_rover_ready",
        "title": "Preflight (dev → Pi)",
        "where": "dev",
        "command": "./scripts/check_rover_ready.sh 192.168.70.11",
        "detail": "Ping, API, camera, Hailo, systemd.",
    },
    {
        "id": "deploy",
        "title": "Deploy to Pi",
        "where": "dev",
        "command": "./deploy_pi.sh",
        "detail": "Rsync code, Hailo link, systemd refresh.",
    },
    {
        "id": "restore_wifi",
        "title": "Restore WiFi from boot config",
        "where": "pi_api",
        "api": "POST /api/maintenance/wifi-restore",
        "detail": "Re-applies GUCZ-744 from /boot/firmware/network-config.",
    },
    {
        "id": "normal_day_power",
        "title": "Normal-day power profile",
        "where": "pi_ssh",
        "command": "sudo bash /opt/rover2/scripts/setup_normal_day_power.sh",
        "detail": "Virtual USB dongle + mask suspend (battery days).",
    },
    {
        "id": "flash_firmware",
        "title": "Flash MegaPi firmware",
        "where": "dev",
        "command": "./scripts/flash_firmware.sh",
        "detail": "Stops rover2-api, avrdude, restarts.",
    },
    {
        "id": "test_plan",
        "title": "Test plan (docs)",
        "where": "docs",
        "path": "docs/TEST_PLAN.md",
        "detail": "Tethered tests today; untethered after keep-alive.",
    },
]


def _status(ok: bool, warn: bool = False) -> str:
    if ok:
        return "ok"
    return "warn" if warn else "error"


def _card(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "detail": detail}
    out.update(extra)
    return out


def _http_ok(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(64).decode("utf-8", errors="replace").strip()
            return resp.status == 200, f"HTTP {resp.status} {body[:40]}".strip()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)[:80]


def _systemd_active(unit: str) -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, "systemctl missing"
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=3,
        )
        active = r.stdout.strip() == "active"
        return active, r.stdout.strip() or r.stderr.strip() or "unknown"
    except Exception as exc:
        return False, str(exc)[:60]


def gather_diagnostics(
    megapi: MegaPiBridge,
    *,
    safety_monitor: SafetyMonitor | None = None,
    body_tracker: BodyTracker | None = None,
    camera_health_url: str = "http://127.0.0.1:8081/health",
    api_port: int = 8082,
) -> dict[str, Any]:
    """Build a diagnostics report (safe to call from a thread)."""
    result: dict[str, Any] = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    hw: dict[str, Any] = {}

    serial_ok = megapi.connected
    hw["megapi"] = _card(
        _status(serial_ok),
        f"port {megapi.port}" if serial_ok else "serial disconnected",
        connected=serial_ok,
        port=megapi.port,
        firmware=megapi.firmware_version,
        motors_ready=megapi.motors_ready,
    )

    ultra_cm: int | None = None
    if serial_ok:
        try:
            ultra_cm = megapi.request_ultrasonic(wait_s=0.7)
        except Exception as exc:
            hw["ultrasonic"] = _card("error", str(exc)[:80])
        else:
            if ultra_cm is None or ultra_cm < 0:
                hw["ultrasonic"] = _card("warn", "no echo or timeout")
            else:
                hw["ultrasonic"] = _card("ok", f"{ultra_cm} cm", cm=ultra_cm)
    else:
        hw["ultrasonic"] = _card("error", "serial not connected")

    cam_ok, cam_detail = _http_ok(camera_health_url)
    hw["camera"] = _card(
        _status(cam_ok),
        cam_detail,
        url=camera_health_url,
        devices=sorted(glob.glob("/dev/video*")),
    )

    if body_tracker is not None:
        st = body_tracker.get_state()
        hailo = bool(st.get("hailo_ready"))
        hw["hailo_follow"] = _card(
            _status(hailo, warn=not hailo and st.get("available")),
            st.get("last_error") or ("ready" if hailo else "starting or unavailable"),
            available=st.get("available"),
            enabled=st.get("enabled"),
            detect_only=st.get("detect_only"),
            person_detected=st.get("person_detected"),
            ble_active=st.get("ble_active"),
        )
        ble = st.get("ble") or {}
        ble_seen = bool(ble.get("seen"))
        hw["ble_beacon"] = _card(
            _status(ble_seen, warn=not ble_seen and ble.get("available")),
            f"rssi={ble.get('rssi')} target_uuid={ble.get('target_uuid')}",
            available=ble.get("available"),
            seen=ble_seen,
            rssi=ble.get("rssi"),
        )
    else:
        hw["hailo_follow"] = _card("skip", "body_tracker not configured")
        hw["ble_beacon"] = _card("skip", "ble not configured")

    if safety_monitor is not None:
        hw["safety"] = _card(
            "ok",
            f"forward block < {safety_monitor.safe_distance_cm} cm",
            forward_blocked=safety_monitor.forward_blocked,
            distance_cm=safety_monitor.distance_cm,
        )
    result["hardware"] = hw

    net: dict[str, Any] = {}
    for iface in ("wlan0", "eth0"):
        try:
            r = subprocess.run(
                ["ip", "-4", "-br", "addr", "show", iface],
                capture_output=True,
                text=True,
                timeout=2,
            )
            line = (r.stdout or "").strip() or "missing"
            up = line.startswith(f"{iface}  UP") or " UP " in line
            net[iface] = _card(_status(up, warn=not up), line[:100])
        except Exception as exc:
            net[iface] = _card("error", str(exc)[:60])
    result["network"] = net

    sw: dict[str, Any] = {}
    for unit in (
        "rover2-api.service",
        "rover-camera.service",
        "rover2-restore-wifi.service",
        "rover2-virtual-usb-dongle.service",
    ):
        active, detail = _systemd_active(unit)
        sw[unit] = _card(_status(active, warn=unit.endswith("virtual-usb-dongle.service") and not active), detail)
    api_ok, api_detail = _http_ok(f"http://127.0.0.1:{api_port}/api/health")
    sw["rover2_api"] = _card(_status(api_ok), api_detail)
    result["software"] = sw

    sys_d: dict[str, Any] = {}
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        temp_c: float | None = None
        for _name, entries in (psutil.sensors_temperatures() or {}).items():
            if entries:
                temp_c = round(entries[0].current, 1)
                break
        sys_d["cpu"] = _card(_status(cpu < 90, warn=cpu >= 90), f"{cpu}%")
        sys_d["ram"] = _card(_status(vm.percent < 90, warn=vm.percent >= 90), f"{vm.percent:.0f}% used")
        sys_d["disk"] = _card(_status(disk.percent < 92, warn=disk.percent >= 92), f"{disk.free // (1024**3)} GB free")
        if temp_c is not None:
            sys_d["temperature"] = _card(_status(temp_c < 80, warn=temp_c >= 80), f"{temp_c} °C")
    except ImportError:
        sys_d["metrics"] = _card("skip", "install python3-psutil for CPU/RAM")
    except Exception as exc:
        sys_d["metrics"] = _card("error", str(exc)[:80])
    result["system"] = sys_d

    return result


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _fmt_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def gather_full_diagnostics(
    megapi: MegaPiBridge,
    *,
    safety_monitor: SafetyMonitor | None = None,
    body_tracker: BodyTracker | None = None,
    camera_health_url: str = "http://127.0.0.1:8081/health",
    api_port: int = 8082,
) -> dict[str, Any]:
    """Full diagnostics: base report + extended system/process/network metrics."""
    import os

    result = gather_diagnostics(
        megapi,
        safety_monitor=safety_monitor,
        body_tracker=body_tracker,
        camera_health_url=camera_health_url,
        api_port=api_port,
    )

    ext: dict[str, Any] = {}
    try:
        import psutil

        # Uptime + load
        uptime_s = int(time.time() - psutil.boot_time())
        load1, load5, load15 = os.getloadavg()
        ext["uptime"] = _fmt_uptime(uptime_s)
        ext["uptime_s"] = uptime_s
        ext["load_avg"] = [round(load1, 2), round(load5, 2), round(load15, 2)]

        # CPU
        cores = psutil.cpu_count(logical=True) or 1
        cpu_overall = psutil.cpu_percent(interval=0.3)
        cpu_per = psutil.cpu_percent(interval=0, percpu=True)
        freq = psutil.cpu_freq()
        ext["cpu_percent"] = cpu_overall
        ext["cpu_cores"] = cores
        ext["cpu_per_core"] = cpu_per
        ext["cpu_freq_mhz"] = round(freq.current) if freq else None

        # RAM
        vm = psutil.virtual_memory()
        ext["ram_percent"] = vm.percent
        ext["ram_used_mb"] = round(vm.used / (1024 ** 2))
        ext["ram_total_mb"] = round(vm.total / (1024 ** 2))

        # Swap
        sw = psutil.swap_memory()
        ext["swap_percent"] = sw.percent
        ext["swap_used_mb"] = round(sw.used / (1024 ** 2))
        ext["swap_total_mb"] = round(sw.total / (1024 ** 2))

        # Disk
        disk = psutil.disk_usage("/")
        ext["disk_percent"] = disk.percent
        ext["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
        ext["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)

        # All temperature sensors
        temps: dict[str, dict[str, Any]] = {}
        for sensor_name, entries in (psutil.sensors_temperatures() or {}).items():
            for e in entries:
                key = f"{sensor_name}/{e.label}" if e.label else sensor_name
                temps[key] = {
                    "current": round(e.current, 1),
                    "high": round(e.high, 1) if e.high else None,
                    "critical": round(e.critical, 1) if e.critical else None,
                }
        ext["temperatures"] = temps

        # Network I/O per interface
        net_io: dict[str, dict[str, str]] = {}
        for iface, c in psutil.net_io_counters(pernic=True).items():
            if iface == "lo":
                continue
            net_io[iface] = {
                "sent": _fmt_bytes(c.bytes_sent),
                "recv": _fmt_bytes(c.bytes_recv),
                "packets_sent": c.packets_sent,
                "packets_recv": c.packets_recv,
            }
        ext["net_io"] = net_io

        # rover2-api process
        pid = os.getpid()
        proc = psutil.Process(pid)
        proc.cpu_percent()  # prime
        time.sleep(0.15)
        ext["process"] = {
            "pid": pid,
            "cpu_percent": proc.cpu_percent(),
            "memory_mb": round(proc.memory_info().rss / (1024 ** 2), 1),
            "threads": proc.num_threads(),
            "status": proc.status(),
        }

    except ImportError:
        ext["error"] = "psutil not available"
    except Exception as exc:
        ext["error"] = str(exc)[:120]

    result["extended"] = ext
    return result


def run_wifi_restore() -> dict[str, Any]:
    """Run restore_wifi_if_needed.sh (root required)."""
    script = "/opt/rover2/scripts/restore_wifi_if_needed.sh"
    if not os.path.isfile(script):
        return {"status": "error", "detail": f"missing {script}"}
    cmd = ["sudo", "-n", "/bin/bash", script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if r.returncode != 0 and "password" in (r.stderr or "").lower():
            return {
                "status": "error",
                "detail": "sudo required — run on Pi: sudo bash /opt/rover2/scripts/restore_wifi_from_boot.sh",
            }
        return {
            "status": "ok" if r.returncode == 0 else "error",
            "returncode": r.returncode,
            "stdout": (r.stdout or "")[-500:],
            "stderr": (r.stderr or "")[-500:],
        }
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:120]}
