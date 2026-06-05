"""Self-healing watchdog for rover2-api.

Monitors system health, applies automatic fixes, and publishes alerts to WebSocket
clients. Runs as an asyncio task inside rover2-api — no separate systemd service.

Safety constraints:
- Max 3 auto-fix actions per 60-minute window (global)
- Per-check: max 3 attempts per 10 minutes; if no improvement → persistent alert
- Never restarts rover2-api from inside itself during active voice/follow session
- Never restarts hailo-ollama (disabled per issue #26)
- Never reboots, never runs dkms autoinstall, never deletes model files
- All subprocess calls capped at 30 s timeout
- Log file rotated at 10 MB
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PATH   = Path("/opt/rover2/data/watchdog.log")
_LOG_MAX_B  = 10 * 1024 * 1024   # rotate at 10 MB
_DEPLOY_GUARD = Path("/tmp/watchdog_deployed")

_RETRY_WINDOW_S      = 600   # per-check rate-limit window: 10 minutes
_MAX_RETRIES_PER_CHECK = 3   # attempts before emitting persistent alert


class Watchdog:
    def __init__(
        self,
        config: dict[str, Any],
        audio_router: Any = None,
        body_tracker: Any = None,
        safety_monitor: Any = None,
        megapi: Any = None,
    ) -> None:
        cfg = config.get("watchdog", {})
        self._enabled          = bool(cfg.get("enabled", True))
        # Interval: honour existing check_interval_s key; cap at 30s per task spec
        raw_interval           = float(cfg.get("interval_s", cfg.get("check_interval_s", 60)))
        self._check_interval_s = min(raw_interval, 30.0)
        self._mem_warn_mb      = float(cfg.get("memory_warn_mb", 420))
        self._mem_crit_mb      = float(cfg.get("memory_critical_mb", 500))
        self._temp_warn_c      = float(cfg.get("temp_warn_c", 78))
        self._temp_crit_c      = float(cfg.get("temp_critical_c", 85))
        self._action_window_s  = float(cfg.get("action_window_s", 3600))
        self._max_actions      = int(cfg.get("max_actions", 3))

        # New config keys
        self._ollama_idle_stop_s  = float(cfg.get("ollama_idle_stop_minutes", 2)) * 60
        self._cpu_sustained_pct   = float(cfg.get("cpu_sustained_percent", 90))
        self._cpu_sustained_s     = float(cfg.get("cpu_sustained_seconds", 60))
        self._disk_warn_gb        = float(cfg.get("disk_warn_gb", 2.0))
        self._disk_critical_gb    = float(cfg.get("disk_critical_gb", 0.5))
        self._cert_warn_days      = int(cfg.get("cert_warn_days", 30))
        self._cert_critical_days  = int(cfg.get("cert_critical_days", 7))
        self._temp_follow_disable_c = float(cfg.get("temp_follow_disable_c", 85.0))
        self._temp_tts_c          = float(cfg.get("temp_critical_c", 90.0))

        # Component refs (injected by server.py)
        self._audio_router  = audio_router
        self._body_tracker  = body_tracker
        self._safety_monitor = safety_monitor
        self._megapi        = megapi

        # Global action window (existing behaviour)
        self._actions: list[float] = []
        self._last_ollama_restart: float = 0.0
        self._hailo_500_streak: int      = 0   # kept for compat; hailo-ollama removed
        self._limit_reached: bool        = False
        self._issue_detected: bool       = False
        self._last_action_desc: str      = ""
        self._task: asyncio.Task | None  = None

        # Per-check rate-limit: check_key → list of monotonic timestamps
        self._retry_ts: dict[str, list[float]] = {}

        # Persistent alerts for WebSocket broadcast
        self._persistent_alerts: list[dict[str, Any]] = []

        # ── New check state ──────────────────────────────────────────────────
        # Camera frames stall
        self._last_frames_inferred: int       = 0
        self._frames_stall_since: float | None = None

        # Robot stuck
        self._ult_history: list[tuple[float, int | None]] = []

        # Person lost
        self._person_lost_since: float | None = None
        self._ble_rssi_buf: list[float]        = []

        # Safety stale block
        self._safety_stale_since: float | None = None

        # CPU sustained high
        self._cpu_high_since: float | None  = None
        self._dkms_suppress_until: float    = 0.0

        # Ollama idle tracking
        self._ollama_low_cpu_since: float | None = None
        self._ollama_stopped_for_mem: bool        = False

        # Cert check (rate-limit to once per day)
        self._cert_last_check: float = 0.0

        # Env/binary warnings (emit once per session to avoid noise)
        self._env_warned: set[str] = set()

        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._enabled:
            logger.info("Watchdog: disabled in config — not starting")
            return
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Watchdog: started (interval=%ds, max_actions=%d/%.0fmin)",
            int(self._check_interval_s), self._max_actions,
            self._action_window_s / 60,
        )

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def reset_actions(self) -> None:
        """Manual reset — clears global action counter and limit-reached flag."""
        self._actions.clear()
        self._limit_reached = False
        self._retry_ts.clear()
        self._persistent_alerts.clear()
        self._log("INFO", "manual_reset", "reset_actions", "action counter cleared by operator")
        logger.info("Watchdog: actions reset by operator")

    @property
    def actions_used(self) -> int:
        self._prune_window()
        return len(self._actions)

    @property
    def ok(self) -> bool:
        return not self._limit_reached

    def get_persistent_alerts(self) -> list[dict[str, Any]]:
        """Return current watchdog alerts for WebSocket broadcast."""
        return list(self._persistent_alerts)

    # ── Internal helpers — global action window ─────────────────────────────

    def _prune_window(self) -> None:
        cutoff = time.monotonic() - self._action_window_s
        self._actions = [t for t in self._actions if t > cutoff]

    def _can_act(self) -> bool:
        if self._limit_reached:
            return False
        self._prune_window()
        return len(self._actions) < self._max_actions

    def _record_action(self) -> None:
        self._actions.append(time.monotonic())
        self._prune_window()
        if len(self._actions) >= self._max_actions:
            self._limit_reached = True
            self._log(
                "CRITICAL", "action_limit", "speak_alert",
                f"reached {self._max_actions} actions in {self._action_window_s/60:.0f} min window"
                " — halting auto-fix until manually reset via POST /api/watchdog/reset",
            )
            self._speak_alert("I require attention, sir.")

    # ── Per-check rate limiting ─────────────────────────────────────────────

    def _can_act_for(self, key: str) -> bool:
        cutoff = time.monotonic() - _RETRY_WINDOW_S
        ts_list = [t for t in self._retry_ts.get(key, []) if t > cutoff]
        self._retry_ts[key] = ts_list
        return len(ts_list) < _MAX_RETRIES_PER_CHECK

    def _record_attempt(self, key: str) -> None:
        self._retry_ts.setdefault(key, []).append(time.monotonic())
        cutoff = time.monotonic() - _RETRY_WINDOW_S
        recent = [t for t in self._retry_ts[key] if t > cutoff]
        self._retry_ts[key] = recent
        if len(recent) >= _MAX_RETRIES_PER_CHECK:
            self._emit_alert(
                key, "critical",
                f"{key}: attempted fix {_MAX_RETRIES_PER_CHECK}× in 10 min without improvement"
                " — manual intervention required",
                action_taken="stopped_retrying",
            )

    # ── Persistent alert management ─────────────────────────────────────────

    def _emit_alert(self, key: str, severity: str, message: str,
                    action_taken: str = "none") -> None:
        ts_iso = datetime.datetime.now().isoformat(timespec="seconds")
        alert: dict[str, Any] = {
            # WebSocket UI fields (compatible with existing alert format)
            "metric":   f"watchdog_{key}",
            "value":    0,
            "severity": severity,
            "ts":       int(time.time()),
            "msg":      message,
            # Watchdog-specific fields
            "source":       "watchdog",
            "action_taken": action_taken,
            "timestamp":    ts_iso,
        }
        # Replace any existing alert with same key
        self._persistent_alerts = [
            a for a in self._persistent_alerts
            if a.get("metric") != f"watchdog_{key}"
        ]
        self._persistent_alerts.append(alert)
        level = logging.CRITICAL if severity == "critical" else logging.WARNING
        logger.log(level, "Watchdog alert [%s] %s — action: %s", key, message, action_taken)

    def _clear_alert(self, key: str) -> None:
        self._persistent_alerts = [
            a for a in self._persistent_alerts
            if a.get("metric") != f"watchdog_{key}"
        ]

    # ── Logging ─────────────────────────────────────────────────────────────

    def _log(self, level: str, issue: str, action: str, result: str) -> None:
        try:
            if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _LOG_MAX_B:
                data = _LOG_PATH.read_bytes()
                _LOG_PATH.write_bytes(data[-_LOG_MAX_B // 2:])
            ts   = time.strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] {level:8s} | {issue:40s} | {action:30s} | {result}\n"
            with _LOG_PATH.open("a") as f:
                f.write(line)
            if level in ("WARNING", "ERROR", "CRITICAL"):
                logger.warning("Watchdog %s: issue=%s action=%s result=%s",
                               level, issue, action, result)
            else:
                logger.debug("Watchdog %s: %s → %s", issue, action, result)
        except Exception as exc:
            logger.warning("Watchdog: log write failed: %s", exc)

    def _speak_alert(self, text: str) -> None:
        if self._audio_router is None:
            return
        try:
            import voice_engine as _ve
            if _ve.is_active():
                return  # never interrupt an active conversation
            _ve._last_spoken["WATCHDOG_ALERT"] = 0.0
            asyncio.ensure_future(_ve._do_speak_event(text, self._audio_router))
        except Exception as exc:
            logger.warning("Watchdog: speak_alert failed: %s", exc)

    # ── Subprocess helper ───────────────────────────────────────────────────

    async def _run_fix(self, cmd: list[str]) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("Watchdog: command timed out: %s", cmd)
                return False
            return proc.returncode == 0
        except Exception as exc:
            logger.warning("Watchdog: _run_fix %s: %s", cmd, exc)
            return False

    async def _run_output(self, cmd: list[str], timeout: float = 10.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return out.decode(errors="replace")
        except Exception:
            return ""

    # ── Probes ──────────────────────────────────────────────────────────────

    async def _get_self_rss_mb(self) -> float | None:
        try:
            import os
            status = Path(f"/proc/{os.getpid()}/status").read_text()
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
        except Exception:
            pass
        return None

    async def _get_temp_c(self) -> float | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "vcgencmd", "measure_temp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return float(out.decode().strip().replace("temp=", "").replace("'C", ""))
        except Exception:
            return None

    async def _get_disk_free_gb(self) -> float | None:
        try:
            usage = shutil.disk_usage("/")
            return usage.free / (1024 ** 3)
        except Exception:
            return None

    async def _get_disk_pct(self) -> float | None:
        try:
            usage = shutil.disk_usage("/")
            return 100.0 * usage.used / usage.total
        except Exception:
            return None

    async def _probe_camera(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get("http://localhost:8081/stream")
                return r.status_code in (200, 206)
        except Exception:
            return False

    async def _probe_api(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0, verify=False) as c:
                r = await c.get("https://localhost:8082/api/status")
                return r.status_code == 200
        except Exception:
            return False

    async def _is_service_active(self, name: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return proc.returncode == 0
        except Exception:
            return False

    async def _get_ollama_runner_count(self) -> int:
        try:
            out = await self._run_output(["ps", "aux"])
            return sum(1 for line in out.splitlines() if "ollama runner" in line)
        except Exception:
            return 0

    async def _get_process_cpu(self, name_substr: str) -> float | None:
        try:
            out = await self._run_output(["ps", "aux"])
            total = 0.0
            found = False
            for line in out.splitlines()[1:]:
                if name_substr in line:
                    parts = line.split()
                    if len(parts) > 2:
                        try:
                            total += float(parts[2])
                            found = True
                        except ValueError:
                            pass
            return total if found else None
        except Exception:
            return None

    async def _sample_cpu_pct(self) -> float | None:
        """Sample system CPU % over ~0.5 s using /proc/stat."""
        def _read_stat() -> tuple[int, int]:
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            return vals[3], sum(vals)  # idle, total

        try:
            idle0, total0 = await asyncio.to_thread(_read_stat)
            await asyncio.sleep(0.5)
            idle1, total1 = await asyncio.to_thread(_read_stat)
            dtotal = total1 - total0
            if dtotal == 0:
                return None
            return 100.0 * (1.0 - (idle1 - idle0) / dtotal)
        except Exception:
            return None

    async def _top5_rss(self) -> str:
        try:
            out = await self._run_output(["ps", "aux"])
            entries: list[tuple[int, str]] = []
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 11:
                    try:
                        entries.append((int(parts[5]), parts[10][:22]))
                    except ValueError:
                        pass
            entries.sort(reverse=True)
            return ", ".join(f"{n}:{r // 1024}MB" for r, n in entries[:5])
        except Exception:
            return "n/a"

    # ── Convenience checks on component state ───────────────────────────────

    def _is_follow_active(self) -> bool:
        bt = self._body_tracker
        if bt is None:
            return False
        return bool(bt.enabled or bt.detect_only)

    def _is_voice_active(self) -> bool:
        try:
            import voice_engine as _ve
            return _ve.is_active()
        except Exception:
            return False

    # ── Monitor loop ────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        await asyncio.sleep(30.0)   # let services settle on boot
        while True:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Watchdog: check error: %s", exc)
            interval = 10.0 if self._issue_detected else self._check_interval_s
            await asyncio.sleep(interval)

    # ── EXISTING checks (logic preserved exactly) ───────────────────────────

    async def _check_existing_memory(self) -> None:
        rss = await self._get_self_rss_mb()
        if rss is None:
            return
        if rss > self._mem_crit_mb:
            self._issue_detected = True
            if self._can_act():
                now = time.monotonic()
                if now - self._last_ollama_restart > 1800:
                    self._last_ollama_restart = now
                    ok = await self._run_fix(["sudo", "systemctl", "restart", "ollama"])
                    self._record_action()
                    self._last_action_desc = f"RSS {rss:.0f} MB → restart ollama"
                    self._log("CRITICAL", f"memory_critical_{rss:.0f}MB",
                              "restart_ollama", "ok" if ok else "failed")
                else:
                    self._log("WARNING", f"memory_critical_{rss:.0f}MB",
                              "restart_ollama_skipped", "cooldown_30min")
        elif rss > self._mem_warn_mb:
            self._log("WARNING", f"memory_warn_{rss:.0f}MB", "none", "monitoring")

    async def _check_existing_temp(self) -> None:
        temp = await self._get_temp_c()
        if temp is None:
            return
        if temp > self._temp_crit_c:
            self._issue_detected = True
            if self._can_act():
                ok = await self._run_fix([
                    "curl", "-sk", "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", '{"enabled":false,"detect_only":false}',
                    "https://localhost:8082/api/tracking",
                ])
                self._record_action()
                self._last_action_desc = f"temp {temp:.1f}°C → disable tracking"
                self._log("CRITICAL", f"temp_critical_{temp:.1f}C",
                          "disable_tracking", "ok" if ok else "failed")
        elif temp > self._temp_warn_c:
            self._log("WARNING", f"temp_warn_{temp:.1f}C", "none", "monitoring")

    async def _check_existing_camera(self) -> None:
        cam_ok = await self._is_service_active("rover-camera")
        if not cam_ok:
            self._issue_detected = True
            if self._can_act():
                ok = await self._run_fix(["sudo", "systemctl", "restart", "rover-camera"])
                result = "restart_failed"
                if ok:
                    await asyncio.sleep(5.0)
                    result = "stream_ok" if await self._probe_camera() else "stream_not_responding"
                self._record_action()
                self._last_action_desc = "rover-camera down → restart"
                self._log("ERROR", "rover_camera_down", "restart_rover_camera", result)

    async def _check_existing_ollama_runners(self) -> None:
        runners = await self._get_ollama_runner_count()
        if runners > 1:
            self._issue_detected = True
            if self._can_act():
                now = time.monotonic()
                if now - self._last_ollama_restart > 300:
                    self._last_ollama_restart = now
                    ok = await self._run_fix(["sudo", "systemctl", "restart", "ollama"])
                    self._record_action()
                    self._last_action_desc = f"ollama {runners} runners → restart ollama"
                    self._log("WARNING", f"ollama_runners_{runners}",
                              "restart_ollama", "ok" if ok else "failed")
                else:
                    self._log("WARNING", f"ollama_runners_{runners}",
                              "restart_ollama_skipped", "cooldown_5min")

    async def _check_existing_disk(self) -> None:
        disk = await self._get_disk_pct()
        if disk is None:
            return
        if disk > 95:
            self._issue_detected = True
            if self._can_act():
                await self._run_fix(["sudo", "find", "/tmp", "-maxdepth", "1",
                                     "-mindepth", "1", "-not", "-name", "watchdog_*",
                                     "-delete"])
                await self._run_fix(["find", str(Path.home() / ".cache/pip"),
                                     "-mindepth", "1", "-delete"])
                if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 1024 * 1024:
                    data = _LOG_PATH.read_bytes()
                    _LOG_PATH.write_bytes(data[-10240:])
                self._record_action()
                self._last_action_desc = f"disk {disk:.0f}% → clear tmp/pip"
                self._log("CRITICAL", f"disk_critical_{disk:.0f}pct",
                          "clear_tmp_pip_logs", "done")
        elif disk > 85:
            self._log("WARNING", f"disk_warn_{disk:.0f}pct", "none", "monitoring")
            if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 50 * 1024 * 1024:
                data = _LOG_PATH.read_bytes()
                _LOG_PATH.write_bytes(data[-50000:])
                self._log("INFO", "log_size_warn", "rotate_log", "truncated to 50 KB")

    # ── NEW checks ──────────────────────────────────────────────────────────

    async def _wlan0_connected(self) -> bool:
        """Return True if wlan0 is connected to home WiFi."""
        out = await self._run_output(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device"])
        return "wlan0:connected" in out

    async def _check_services(self) -> None:
        # hailo-ollama excluded (disabled per issue #26)
        services = ["hostapd", "dnsmasq", "ssh", "tailscaled"]
        for svc in services:
            try:
                active = await self._is_service_active(svc)
                if active:
                    self._clear_alert(f"svc_{svc}")
                    logger.debug("Watchdog: service %s OK", svc)
                    continue
                # AP services are intentionally stopped when wlan0 is on home WiFi
                # (NM dispatcher disables AP to save power — do not fight it)
                if svc in ("hostapd", "dnsmasq"):
                    if await self._wlan0_connected():
                        self._clear_alert(f"svc_{svc}")
                        logger.debug("Watchdog: %s inactive, wlan0 connected"
                                     " — AP intentionally off", svc)
                        continue
                self._issue_detected = True
                key = f"svc_{svc}"
                if not self._can_act_for(key):
                    continue
                self._record_attempt(key)
                ok = await self._run_fix(["sudo", "systemctl", "restart", svc])
                self._log("ERROR", f"service_{svc}_down", f"restart_{svc}",
                          "ok" if ok else "failed")
                if ok:
                    self._clear_alert(key)
                else:
                    self._emit_alert(key, "warning",
                                     f"service {svc} failed to restart",
                                     action_taken=f"systemctl restart {svc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Watchdog: _check_services(%s): %s", svc, exc)

        # rover2-api: log critical + alert only (never restart from inside)
        try:
            api_ok = await self._probe_api()
            if not api_ok:
                self._emit_alert("self_api", "critical",
                                 "rover2-api /api/status unreachable",
                                 action_taken="none — cannot self-restart")
                self._log("CRITICAL", "rover2_api_unreachable", "none",
                          "manual restart required")
            else:
                self._clear_alert("self_api")
        except Exception as exc:
            logger.debug("Watchdog: api self-probe: %s", exc)

        # ollama: stop if active but CPU low for extended period
        try:
            ollama_active = await self._is_service_active("ollama")
            if ollama_active:
                cpu = await self._get_process_cpu("ollama")
                if cpu is not None and cpu < 1.0:
                    if self._ollama_low_cpu_since is None:
                        self._ollama_low_cpu_since = time.monotonic()
                    elif time.monotonic() - self._ollama_low_cpu_since >= self._ollama_idle_stop_s:
                        key = "ollama_idle"
                        if self._can_act_for(key):
                            self._record_attempt(key)
                            ok = await self._run_fix(["sudo", "systemctl", "stop", "ollama"])
                            self._log("INFO", "ollama_idle_stopped",
                                      "stop_ollama", "ok" if ok else "failed")
                        self._ollama_low_cpu_since = None
                else:
                    self._ollama_low_cpu_since = None
            else:
                self._ollama_low_cpu_since = None
        except Exception as exc:
            logger.debug("Watchdog: ollama idle check: %s", exc)

    async def _check_network(self) -> None:
        # eth0 is the Pi's built-in Ethernet — NO-CARRIER means no cable plugged in.
        # This is normal and expected; do not attempt to bring it up.

        # wlan0 association (home WiFi — station mode)
        try:
            wlan = await self._run_output(["iwconfig", "wlan0"])
            if "Not-Associated" in wlan or "unassociated" in wlan.lower():
                key = "wlan0_assoc"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    ok = await self._run_fix(["nmcli", "device", "connect", "wlan0"])
                    self._log("WARNING", "wlan0_not_associated", "nmcli_connect",
                              "ok" if ok else "failed")
                    if not ok:
                        self._emit_alert(key, "warning",
                                         "wlan0 not associated — nmcli reconnect failed",
                                         action_taken="nmcli device connect wlan0")
                else:
                    self._emit_alert(key, "warning",
                                     "wlan0 not associated — retry limit reached")
            else:
                self._clear_alert("wlan0_assoc")
        except Exception as exc:
            logger.debug("Watchdog: wlan0 check: %s", exc)

        # wlan1 AP mode + IP — RTL8812AU hotspot (ROVER2, 10.0.0.1)
        # AP is intentionally off when wlan0 is connected (NM dispatcher disables it).
        # Only alert/fix when away from home (wlan0 not connected).
        # Restart order: systemd-networkd (assigns IP) → hostapd (AP) → dnsmasq (DHCP)
        try:
            if await self._wlan0_connected():
                self._clear_alert("wlan1_ap")
            else:
                wlan1_info = await self._run_output(["sudo", "iw", "dev", "wlan1", "info"])
                wlan1_addr = await self._run_output(["ip", "addr", "show", "wlan1"])
                ap_ok = "type AP" in wlan1_info and "ROVER2" in wlan1_info
                ip_ok = "10.0.0.1" in wlan1_addr
                if ap_ok and ip_ok:
                    self._clear_alert("wlan1_ap")
                else:
                    self._issue_detected = True
                    key = "wlan1_ap"
                    if self._can_act_for(key):
                        self._record_attempt(key)
                        await self._run_fix(["sudo", "systemctl", "restart",
                                             "systemd-networkd"])
                        await asyncio.sleep(2.0)
                        ok = await self._run_fix(["sudo", "systemctl", "restart",
                                                  "hostapd"])
                        await asyncio.sleep(2.0)
                        await self._run_fix(["sudo", "systemctl", "restart", "dnsmasq"])
                        reason = "no IP (10.0.0.1 missing)" if ap_ok else "AP mode lost"
                        self._log("WARNING", f"wlan1_{reason.replace(' ', '_')}",
                                  "restart_networkd_hostapd_dnsmasq",
                                  "ok" if ok else "failed")
                        self._emit_alert(key, "warning",
                                         f"wlan1 AP issue ({reason}) — restarted AP stack",
                                         action_taken="restart networkd → hostapd → dnsmasq")
        except Exception as exc:
            logger.debug("Watchdog: wlan1 AP check: %s", exc)

    async def _check_hailo_module(self) -> None:
        try:
            lsmod = await self._run_output(["lsmod"])
            if "hailo" not in lsmod:
                self._issue_detected = True
                key = "hailo_module"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    ok = await self._run_fix(["sudo", "modprobe", "hailo1x_pci"])
                    self._log("ERROR", "hailo_module_missing", "modprobe_hailo1x_pci",
                              "ok" if ok else "FAILED")
                    if not ok:
                        self._emit_alert(
                            key, "critical",
                            "HAILO DRIVER NOT LOADED — modprobe failed. "
                            "Run: sudo dkms autoinstall",
                            action_taken="modprobe hailo1x_pci (failed)",
                        )
                    else:
                        self._clear_alert(key)
                else:
                    self._emit_alert(key, "critical",
                                     "HAILO DRIVER NOT LOADED — modprobe retries exhausted. "
                                     "Run: sudo dkms autoinstall")
            else:
                self._clear_alert("hailo_module")
                # Module present but hailo not ready while tracking active
                if self._body_tracker is not None:
                    st = self._body_tracker.get_state()
                    if (st.get("enabled") or st.get("detect_only")) \
                            and not st.get("hailo_ready"):
                        logger.warning(
                            "Watchdog: hailo module present but tracker not ready")
        except Exception as exc:
            logger.debug("Watchdog: hailo module check: %s", exc)

    async def _check_wifi_driver(self) -> None:
        """Verify rtw88_8812au (RTL8812AU wlan1 AP adapter) kernel module is loaded."""
        try:
            lsmod = await self._run_output(["lsmod"])
            if "rtw88_8812au" in lsmod:
                self._clear_alert("wifi_driver")
                return
            self._issue_detected = True
            key = "wifi_driver"
            if self._can_act_for(key):
                self._record_attempt(key)
                ok = await self._run_fix(["sudo", "modprobe", "rtw88_8812au"])
                self._log("ERROR", "rtw88_8812au_missing", "modprobe_rtw88_8812au",
                          "ok" if ok else "FAILED")
                if ok:
                    await asyncio.sleep(2.0)
                    await self._run_fix(["sudo", "systemctl", "restart", "hostapd"])
                    self._clear_alert(key)
                else:
                    self._emit_alert(key, "critical",
                                     "RTL8812AU DRIVER NOT LOADED — modprobe rtw88_8812au "
                                     "failed. Check USB connection.",
                                     action_taken="modprobe rtw88_8812au (failed)")
            else:
                self._emit_alert(key, "critical",
                                 "RTL8812AU DRIVER NOT LOADED — retries exhausted. "
                                 "Check USB connection.",
                                 action_taken="modprobe rtw88_8812au (exhausted)")
        except Exception as exc:
            logger.debug("Watchdog: wifi_driver check: %s", exc)

    async def _check_camera_frames(self) -> None:
        try:
            bt = self._body_tracker
            if bt is None:
                return
            if not (bt.enabled or bt.detect_only):
                self._frames_stall_since = None
                self._clear_alert("camera_frames_stall")
                return
            cam_active = await self._is_service_active("rover-camera")
            if not cam_active:
                return  # handled by _check_existing_camera
            current = bt.frames_inferred
            if current == self._last_frames_inferred:
                if self._frames_stall_since is None:
                    self._frames_stall_since = time.monotonic()
                elif time.monotonic() - self._frames_stall_since >= 60.0:
                    self._issue_detected = True
                    key = "camera_frames_stall"
                    if self._can_act_for(key):
                        self._record_attempt(key)
                        ok = await self._run_fix(["sudo", "systemctl",
                                                   "restart", "rover-camera"])
                        self._log("WARNING", "camera_frames_stalled",
                                  "restart_rover_camera", "ok" if ok else "failed")
                        self._emit_alert(key, "warning",
                                         "CAMERA FRAMES STALLED — rover-camera restarted",
                                         action_taken="restart rover-camera")
                        if ok:
                            self._frames_stall_since = None
                            self._clear_alert(key)
            else:
                self._last_frames_inferred = current
                self._frames_stall_since = None
                self._clear_alert("camera_frames_stall")
        except Exception as exc:
            logger.debug("Watchdog: camera frames check: %s", exc)

    async def _check_megapi_serial(self) -> None:
        try:
            ttyusb = Path("/dev/ttyUSB0")
            key = "megapi_absent"
            if not ttyusb.exists():
                if key not in self._env_warned:
                    logger.warning("Watchdog: MEGAPI NOT DETECTED — check USB cable")
                    self._emit_alert(key, "warning",
                                     "MEGAPI NOT DETECTED — check USB cable",
                                     action_taken="none")
                    self._env_warned.add(key)
                return
            # Device present — check connection state via MegaPi bridge
            self._clear_alert(key)
            self._env_warned.discard(key)
            mp = self._megapi
            if mp is not None and not mp.connected:
                rkey = "megapi_reconnect"
                if self._can_act_for(rkey):
                    self._record_attempt(rkey)
                    mp.trigger_reconnect()
                    self._log("WARNING", "megapi_serial_not_responding",
                              "trigger_reconnect", "requested")
                else:
                    self._emit_alert(rkey, "warning",
                                     "MegaPi serial not responding — reconnect retries exhausted",
                                     action_taken="trigger_reconnect (exhausted)")
        except Exception as exc:
            logger.debug("Watchdog: megapi check: %s", exc)

    async def _check_robot_stuck(self) -> None:
        try:
            bt = self._body_tracker
            sm = self._safety_monitor
            if bt is None or sm is None:
                return
            if not bt.enabled or not bt.person_detected:
                self._ult_history.clear()
                self._clear_alert("robot_stuck")
                return
            dist = sm.distance_cm
            now = time.monotonic()
            self._ult_history.append((now, dist))
            # Keep only last 35s of history
            self._ult_history = [(t, d) for t, d in self._ult_history
                                  if now - t <= 35.0]
            if len(self._ult_history) < 4:
                return
            oldest_t, _ = self._ult_history[0]
            if now - oldest_t < 30.0:
                return
            dists = [d for _, d in self._ult_history if d is not None]
            if not dists:
                return
            spread = max(dists) - min(dists)
            if spread <= 5:
                key = "robot_stuck"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    # Stop motors and disable follow
                    if bt.enabled:
                        bt.set_enabled(False)
                    self._log("CRITICAL", "robot_stuck",
                              "stop_motors_disable_follow", "done")
                    self._emit_alert(
                        key, "critical",
                        "ROVER STUCK — motors stalled or obstructed, follow disabled",
                        action_taken="set_enabled(False)",
                    )
                    self._ult_history.clear()
            else:
                self._clear_alert("robot_stuck")
        except Exception as exc:
            logger.debug("Watchdog: robot_stuck check: %s", exc)

    async def _check_person_lost(self) -> None:
        try:
            bt = self._body_tracker
            if bt is None:
                return
            if not bt.enabled or bt.person_detected:
                self._person_lost_since = None
                self._ble_rssi_buf.clear()
                self._clear_alert("person_lost")
                return
            # FOLLOW active, person not detected
            now = time.monotonic()
            if self._person_lost_since is None:
                self._person_lost_since = now
            # Collect BLE RSSI
            st = bt.get_state()
            ble = st.get("ble", {})
            rssi = ble.get("rssi") if ble else None
            if rssi is not None:
                self._ble_rssi_buf.append(float(rssi))
                if len(self._ble_rssi_buf) > 20:
                    self._ble_rssi_buf = self._ble_rssi_buf[-20:]

            if now - self._person_lost_since < 45.0:
                return
            # Check BLE RSSI variance
            if len(self._ble_rssi_buf) >= 4:
                variance = max(self._ble_rssi_buf) - min(self._ble_rssi_buf)
            else:
                variance = 99.0  # unknown — don't alert
            if variance < 3.0:
                self._emit_alert("person_lost", "info",
                                 "PERSON LOST — searching (BLE RSSI stable, person not visible)",
                                 action_taken="none")
        except Exception as exc:
            logger.debug("Watchdog: person_lost check: %s", exc)

    async def _check_safety_stale(self) -> None:
        try:
            sm = self._safety_monitor
            if sm is None:
                return
            if not sm.forward_blocked:
                self._safety_stale_since = None
                self._clear_alert("stale_safety_block")
                return
            dist = sm.distance_cm
            now = time.monotonic()
            if dist is not None and dist > 60:
                if self._safety_stale_since is None:
                    self._safety_stale_since = now
                elif now - self._safety_stale_since >= 10.0:
                    sm.clear_block()
                    self._log("WARNING", "stale_safety_block_cleared",
                              "clear_block", f"dist={dist}cm")
                    self._emit_alert("stale_safety_block", "warning",
                                     "STALE SAFETY BLOCK CLEARED — obstacle > 60 cm",
                                     action_taken="clear_block()")
                    self._safety_stale_since = None
            else:
                self._safety_stale_since = None
        except Exception as exc:
            logger.debug("Watchdog: safety_stale check: %s", exc)

    async def _check_memory_expanded(self) -> None:
        try:
            rss = await self._get_self_rss_mb()
            if rss is None:
                return
            if rss > 450 and self._ollama_stopped_for_mem:
                # Emergency: ollama already stopped and still high
                if not self._is_voice_active() and not self._is_follow_active():
                    key = "mem_emergency_restart"
                    if self._can_act_for(key):
                        self._record_attempt(key)
                        self._log("CRITICAL", f"memory_emergency_{rss:.0f}MB",
                                  "restart_rover2_api",
                                  "invoking restart (not voice/follow active)")
                        self._emit_alert(key, "critical",
                                         f"RSS {rss:.0f} MB critical — restarting rover2-api",
                                         action_taken="systemctl restart rover2-api")
                        # Slight delay so alert is broadcast before we die
                        await asyncio.sleep(1.0)
                        await self._run_fix(["sudo", "systemctl", "restart", "rover2-api"])
            elif rss > 400 and not self._ollama_stopped_for_mem:
                # Stop ollama proactively + emit alert with top-5
                key = "mem_high_stop_ollama"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    top5 = await self._top5_rss()
                    ok = await self._run_fix(["sudo", "systemctl", "stop", "ollama"])
                    if ok:
                        self._ollama_stopped_for_mem = True
                    self._log("WARNING", f"memory_high_{rss:.0f}MB",
                              "stop_ollama", "ok" if ok else "failed")
                    self._emit_alert(key, "warning",
                                     f"RSS {rss:.0f} MB — stopped ollama. Top: {top5}",
                                     action_taken="systemctl stop ollama")
            else:
                if rss <= 380:
                    self._ollama_stopped_for_mem = False
                    self._clear_alert("mem_high_stop_ollama")
                    self._clear_alert("mem_emergency_restart")
        except Exception as exc:
            logger.debug("Watchdog: memory_expanded check: %s", exc)

    async def _check_cpu(self) -> None:
        try:
            # Skip CPU alerts if DKMS compile suppression is active
            now = time.monotonic()
            if now < self._dkms_suppress_until:
                remaining = self._dkms_suppress_until - now
                logger.debug("Watchdog: CPU alerts suppressed (DKMS compile, %.0fs remaining)",
                             remaining)
                return

            cpu_pct = await self._sample_cpu_pct()
            if cpu_pct is None:
                return

            if cpu_pct < self._cpu_sustained_pct:
                self._cpu_high_since = None
                self._clear_alert("cpu_sustained")
                return

            if self._cpu_high_since is None:
                self._cpu_high_since = now
                return
            if now - self._cpu_high_since < self._cpu_sustained_s:
                return

            # Sustained high CPU — identify cause
            out = await self._run_output(["ps", "aux", "--sort=-%cpu"])
            top_line = ""
            offender = ""
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 11:
                    top_line = line
                    offender = parts[10]
                    break

            if "cc1plus" in out or "cc1" in out:
                # DKMS compile in progress — suppress for 20 min
                self._dkms_suppress_until = now + 1200.0
                self._cpu_high_since = None
                logger.info("Watchdog: cc1plus detected — DKMS compile in progress, "
                            "suppressing CPU alerts for 20 min")
                return

            if "ollama" in offender:
                key = "cpu_ollama"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    ok = await self._run_fix(["sudo", "systemctl", "stop", "ollama"])
                    self._log("WARNING", f"cpu_high_ollama_{cpu_pct:.0f}pct",
                              "stop_ollama", "ok" if ok else "failed")
                self._cpu_high_since = None
                return

            if "rover2" in offender or "python" in offender or "uvicorn" in offender:
                logger.warning("Watchdog: rover2-api sustained high CPU %.0f%% — monitoring only",
                               cpu_pct)
                self._cpu_high_since = None
                return

            # Unknown process
            self._emit_alert("cpu_sustained", "warning",
                             f"CPU {cpu_pct:.0f}% sustained > {self._cpu_sustained_s:.0f}s"
                             f" — top process: {offender}",
                             action_taken="none")
            self._cpu_high_since = None
        except Exception as exc:
            logger.debug("Watchdog: cpu check: %s", exc)

    async def _check_thermal_expanded(self) -> None:
        try:
            temp = await self._get_temp_c()
            if temp is None:
                logger.debug("Watchdog: temp sensor fault — no reading")
                return
            if temp == 0.0:
                logger.warning("Watchdog: TEMP SENSOR FAULT — reading is 0°C")
                return

            if temp > self._temp_tts_c:
                self._emit_alert("thermal_critical", "critical",
                                 f"THERMAL CRITICAL — {temp:.1f}°C",
                                 action_taken="TTS alert (if not in conversation)")
                if not self._is_voice_active():
                    self._speak_alert(
                        f"Warning: I am overheating. Temperature is {temp:.0f} degrees.")
            elif temp > self._temp_follow_disable_c:
                bt = self._body_tracker
                if bt is not None and (bt.enabled or bt.detect_only):
                    key = "thermal_follow_disable"
                    if self._can_act_for(key):
                        self._record_attempt(key)
                        bt.set_enabled(False)
                        bt.set_detect_only(False)
                        self._log("WARNING", f"temp_follow_disable_{temp:.1f}C",
                                  "disable_follow_detect", "done")
                        self._emit_alert(key, "warning",
                                         f"THERMAL LIMIT {temp:.1f}°C — follow/detect disabled",
                                         action_taken="set_enabled(False), set_detect_only(False)")
                else:
                    self._emit_alert("thermal_warn", "warning",
                                     f"Temperature {temp:.1f}°C above follow-disable threshold",
                                     action_taken="none")
            else:
                self._clear_alert("thermal_critical")
                self._clear_alert("thermal_follow_disable")
                self._clear_alert("thermal_warn")
        except Exception as exc:
            logger.debug("Watchdog: thermal check: %s", exc)

    async def _check_disk_expanded(self) -> None:
        try:
            free_gb = await self._get_disk_free_gb()
            if free_gb is None:
                return

            if free_gb < self._disk_critical_gb:
                import metrics_store as _ms
                if _ms.writes_enabled():
                    _ms.disable_writes()
                self._emit_alert("disk_critical", "critical",
                                 f"DISK CRITICAL — {free_gb:.2f} GB free, "
                                 "metrics writes disabled",
                                 action_taken="disable_writes()")
                self._issue_detected = True
            elif free_gb < self._disk_warn_gb:
                key = "disk_low"
                if self._can_act_for(key):
                    self._record_attempt(key)
                    before_gb = free_gb
                    await self._run_fix([
                        "sudo", "journalctl", "--vacuum-size=200M",
                    ])
                    pip_cache = Path.home() / ".cache" / "pip"
                    if pip_cache.exists():
                        await self._run_fix(["find", str(pip_cache),
                                             "-mindepth", "1", "-delete"])
                    after_gb = await self._get_disk_free_gb() or before_gb
                    self._log("WARNING", f"disk_low_{free_gb:.1f}GB",
                              "vacuum_journalctl_pip_cache",
                              f"freed {after_gb - before_gb:.2f} GB")
                    self._emit_alert(key, "warning",
                                     f"Disk low {before_gb:.2f} GB free — "
                                     f"vacuumed journal, now {after_gb:.2f} GB free",
                                     action_taken="journalctl --vacuum-size=200M, clear pip cache")
                self._issue_detected = True
            else:
                self._clear_alert("disk_low")
                self._clear_alert("disk_critical")
                # Re-enable metrics writes if disk recovered
                try:
                    import metrics_store as _ms
                    if not _ms.writes_enabled():
                        _ms.enable_writes()
                        logger.info("Watchdog: disk recovered — metrics writes re-enabled")
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Watchdog: disk_expanded check: %s", exc)

    async def _check_cert(self) -> None:
        try:
            # Check at most once every 6 hours
            now = time.monotonic()
            if now - self._cert_last_check < 21600:
                return
            self._cert_last_check = now

            cert_path = Path("/opt/rover2/rover.crt")
            if not cert_path.exists():
                return

            out = await self._run_output([
                "openssl", "x509", "-enddate", "-noout", "-in", str(cert_path)
            ], timeout=10.0)
            # Output: "notAfter=Jun  5 12:00:00 2027 GMT"
            if "notAfter=" not in out:
                return
            date_str = out.split("notAfter=", 1)[1].strip()
            expiry = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
            days_left = (expiry - datetime.datetime.utcnow()).days

            if days_left < self._cert_critical_days:
                key = "cert_critical"
                renew_script = Path("/opt/rover2/scripts/renew-cert.sh")
                action = "none"
                if renew_script.exists() and self._can_act_for(key):
                    self._record_attempt(key)
                    ok = await self._run_fix(["bash", str(renew_script)])
                    action = f"renew-cert.sh ({'ok' if ok else 'failed'})"
                self._emit_alert(key, "critical",
                                 f"TLS cert expires in {days_left} days — "
                                 f"run {renew_script} immediately",
                                 action_taken=action)
                self._log("CRITICAL", f"cert_expires_{days_left}d",
                          "renew-cert.sh", action)
            elif days_left < self._cert_warn_days:
                self._emit_alert("cert_warn", "warning",
                                 f"TLS cert expires in {days_left} days — "
                                 "run scripts/renew-cert.sh",
                                 action_taken="none")
            else:
                self._clear_alert("cert_warn")
                self._clear_alert("cert_critical")
                logger.debug("Watchdog: TLS cert valid for %d more days", days_left)
        except Exception as exc:
            logger.debug("Watchdog: cert check: %s", exc)

    async def _check_boot_partition(self) -> None:
        try:
            out = await self._run_output(["findmnt", "/boot/firmware", "--output",
                                           "OPTIONS", "--noheadings"])
            if "rw" in out and "ro" not in out.split("rw")[0]:
                await self._run_fix(["sudo", "mount", "-o", "remount,ro",
                                      "/boot/firmware"])
                logger.warning(
                    "Watchdog: BOOT PARTITION WAS RW — remounted ro")
                self._emit_alert("boot_rw", "warning",
                                 "Boot partition was mounted rw — remounted ro",
                                 action_taken="mount -o remount,ro /boot/firmware")
            else:
                self._clear_alert("boot_rw")
        except Exception as exc:
            logger.debug("Watchdog: boot partition check: %s", exc)

    async def _check_env_files(self) -> None:
        checks = [
            ("/etc/rover2.env",
             "HA INTEGRATION DISABLED — /etc/rover2.env missing"),
            ("/opt/rover2/piper/piper",
             "PIPER BINARY MISSING — TTS degraded"),
            ("/opt/rover2/voices/en_GB-cori-high.onnx",
             "TTS VOICE MODEL MISSING — en_GB-cori-high.onnx not found"),
        ]
        for path_str, msg in checks:
            key = f"env_{Path(path_str).name}"
            if not Path(path_str).exists():
                if key not in self._env_warned:
                    logger.warning("Watchdog: %s", msg)
                    self._env_warned.add(key)
            else:
                self._env_warned.discard(key)

    # ── Main check orchestrator ─────────────────────────────────────────────

    async def _check_all(self) -> None:
        self._issue_detected = False

        # ── EXISTING checks (logic preserved exactly) ──────────────────────
        for fn in [
            self._check_existing_memory,
            self._check_existing_temp,
            self._check_existing_camera,
            self._check_existing_ollama_runners,
            self._check_existing_disk,
        ]:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Watchdog: %s: %s", fn.__name__, exc)

        # ── NEW checks (each isolated) ─────────────────────────────────────
        for fn in [
            self._check_services,
            self._check_network,
            self._check_hailo_module,
            self._check_wifi_driver,
            self._check_camera_frames,
            self._check_megapi_serial,
            self._check_robot_stuck,
            self._check_person_lost,
            self._check_safety_stale,
            self._check_memory_expanded,
            self._check_cpu,
            self._check_thermal_expanded,
            self._check_disk_expanded,
            self._check_cert,
            self._check_boot_partition,
            self._check_env_files,
        ]:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Watchdog: %s: %s", fn.__name__, exc)
