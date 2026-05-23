"""On-demand Pi thermal monitoring — no background threads, no polling."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# vcgencmd get_throttled bitmask
_CURRENT_BITS = 0x7      # bits 0–2: undervolted | arm_freq_capped | throttled (now)
_EVER_BITS    = 0x70000  # bits 16–18: same but since-boot

_WARN_DEDUPE_S = 600  # suppress repeated "warm" alerts within 10 min


def _level(temp_c: float) -> str:
    if temp_c >= 85.0:
        return "critical"
    if temp_c >= 80.0:
        return "hot"
    if temp_c >= 75.0:
        return "warm"
    return "cool"


def read_thermal() -> dict[str, Any]:
    """Read CPU temperature and throttle flags. All subprocess calls have explicit timeouts."""
    result: dict[str, Any] = {}

    # Temperature via vcgencmd (primary)
    try:
        r = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=2,
        )
        raw = r.stdout.strip()  # "temp=54.3'C"
        if "=" in raw:
            temp_str = raw.split("=", 1)[1].replace("'C", "").strip()
            result["temp_c"] = round(float(temp_str), 1)
    except Exception:
        pass

    # Sysfs fallback
    if "temp_c" not in result:
        try:
            millideg = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            result["temp_c"] = round(int(millideg) / 1000.0, 1)
        except Exception:
            pass

    # Throttle flags via vcgencmd
    try:
        r = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2,
        )
        raw = r.stdout.strip()  # "throttled=0x0"
        if "=" in raw:
            hex_str = raw.split("=", 1)[1].strip()
            flags = int(hex_str, 16)
            result["throttle_hex"] = hex_str
            result["throttle_current"] = bool(flags & _CURRENT_BITS)
            result["throttle_ever"] = bool(flags & _EVER_BITS)
    except Exception:
        pass

    if "temp_c" in result:
        result["level"] = _level(result["temp_c"])

    return result


def read_cpu_freq_mhz() -> int | None:
    """Read current CPU frequency from sysfs (no subprocess)."""
    try:
        raw = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read_text().strip()
        return round(int(raw) / 1000)
    except Exception:
        return None


def _set_cpu_max_freq(freq_hz: int) -> bool:
    """Write scaling_max_freq for cores 0–3 via sudo tee."""
    ok = True
    for core in range(4):
        path = f"/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_max_freq"
        try:
            r = subprocess.run(
                ["sudo", "tee", path],
                input=str(freq_hz),
                text=True,
                capture_output=True,
                timeout=3,
            )
            if r.returncode != 0:
                ok = False
        except Exception:
            ok = False
    return ok


def check_sudo_tee_available() -> bool:
    """Test that sudo tee works on scaling_max_freq (call at startup)."""
    path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"
    try:
        current = Path(path).read_text().strip()
        r = subprocess.run(
            ["sudo", "tee", path],
            input=current,
            text=True,
            capture_output=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


class ThermalMonitor:
    """Evaluate thermal state on each /api/services call — no background thread."""

    _THROTTLE_FREQ_HZ = 1_600_000
    _RESTORE_FREQ_HZ  = 1_800_000

    def __init__(self) -> None:
        self._last_warm_ts: float         = 0.0
        self._throttled: bool             = False
        self._sudo_available: bool | None = None

    def _check_sudo(self) -> bool:
        if self._sudo_available is None:
            self._sudo_available = check_sudo_tee_available()
            if not self._sudo_available:
                logger.warning(
                    "ThermalMonitor: sudo tee unavailable — auto-throttle disabled. "
                    "Add to sudoers: NOPASSWD: /usr/bin/tee /sys/.../scaling_max_freq"
                )
        return bool(self._sudo_available)

    def evaluate(self, thermal: dict[str, Any]) -> dict[str, Any] | None:
        """Return an alert dict if warranted, else None. Side-effect: applies CPU throttle."""
        level  = thermal.get("level", "cool")
        temp_c = thermal.get("temp_c")
        if temp_c is None:
            return None

        # Auto-throttle / de-throttle
        if level in ("hot", "critical"):
            if not self._throttled and self._check_sudo():
                if _set_cpu_max_freq(self._THROTTLE_FREQ_HZ):
                    self._throttled = True
                    logger.warning(
                        "ThermalMonitor: CPU throttled to %d MHz at %.1f C",
                        self._THROTTLE_FREQ_HZ // 1000, temp_c,
                    )
        elif level in ("cool", "warm") and self._throttled:
            if self._check_sudo() and _set_cpu_max_freq(self._RESTORE_FREQ_HZ):
                self._throttled = False
                logger.info(
                    "ThermalMonitor: CPU de-throttled to %d MHz at %.1f C",
                    self._RESTORE_FREQ_HZ // 1000, temp_c,
                )

        now = time.monotonic()

        if level == "critical":
            return {
                "metric": "temperature",
                "value": round(temp_c, 1),
                "severity": "crit",
                "ts": int(time.time()),
                "msg": f"CPU temperature CRITICAL {temp_c:.1f} C — throttling applied",
            }
        if level == "hot":
            return {
                "metric": "temperature",
                "value": round(temp_c, 1),
                "severity": "warn",
                "ts": int(time.time()),
                "msg": f"CPU temperature hot {temp_c:.1f} C — throttling applied",
            }
        if level == "warm" and now - self._last_warm_ts >= _WARN_DEDUPE_S:
            self._last_warm_ts = now
            return {
                "metric": "temperature",
                "value": round(temp_c, 1),
                "severity": "warn",
                "ts": int(time.time()),
                "msg": f"CPU temperature elevated {temp_c:.1f} C",
            }
        return None
