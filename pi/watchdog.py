"""Self-healing watchdog for rover2-api.

Monitors system health, applies automatic fixes, and logs all actions.
Runs as an asyncio task inside rover2-api — no separate systemd service.

Safety constraints:
- Max 3 auto-fix actions per 60-minute window
- Never restarts rover2-api from inside itself (would kill the watchdog)
- All subprocess calls capped at 30 s timeout
- Log file rotated at 10 MB
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PATH   = Path("/opt/rover2/data/watchdog.log")
_LOG_MAX_B  = 10 * 1024 * 1024   # rotate at 10 MB
_DEPLOY_GUARD = Path("/tmp/watchdog_deployed")


class Watchdog:
    def __init__(
        self,
        config: dict[str, Any],
        audio_router: Any = None,   # AudioRouter | None
    ) -> None:
        cfg = config.get("watchdog", {})
        self._enabled          = bool(cfg.get("enabled", True))
        self._check_interval_s = float(cfg.get("check_interval_s", 60))
        self._mem_warn_mb      = float(cfg.get("memory_warn_mb", 420))
        self._mem_crit_mb      = float(cfg.get("memory_critical_mb", 500))
        self._temp_warn_c      = float(cfg.get("temp_warn_c", 78))
        self._temp_crit_c      = float(cfg.get("temp_critical_c", 85))
        self._action_window_s  = float(cfg.get("action_window_s", 3600))
        self._max_actions      = int(cfg.get("max_actions", 3))

        self._audio_router     = audio_router
        self._actions: list[float] = []   # monotonic timestamps of actions taken
        self._last_ollama_restart: float  = 0.0
        self._hailo_500_streak: int       = 0
        self._limit_reached: bool         = False
        self._issue_detected: bool        = False
        self._last_action_desc: str       = ""
        self._task: asyncio.Task | None   = None

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
        """Manual reset — clears action counter and limit-reached flag."""
        self._actions.clear()
        self._limit_reached = False
        self._log("INFO", "manual_reset", "reset_actions", "action counter cleared by operator")
        logger.info("Watchdog: actions reset by operator")

    @property
    def actions_used(self) -> int:
        self._prune_window()
        return len(self._actions)

    @property
    def ok(self) -> bool:
        return not self._limit_reached

    # ── Internal helpers ────────────────────────────────────────────────────

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
            # Bypass debounce — this is an urgent alert
            _ve._last_spoken["WATCHDOG_ALERT"] = 0.0
            asyncio.ensure_future(_ve._do_speak_event(text, self._audio_router))
        except Exception as exc:
            logger.warning("Watchdog: speak_alert failed: %s", exc)

    async def _run_fix(self, cmd: list[str]) -> bool:
        """Run a fix command with a 30 s hard timeout. Returns True on success."""
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

    async def _get_disk_pct(self) -> float | None:
        try:
            usage = shutil.disk_usage("/")
            return 100.0 * usage.used / usage.total
        except Exception:
            return None

    async def _probe_hailo_ollama(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(
                    "http://localhost:8000/api/generate",
                    json={"model": "qwen2.5-instruct:1.5b", "prompt": "hi",
                          "stream": False, "options": {"num_predict": 1}},
                )
                return r.status_code == 200
        except Exception:
            return False

    async def _probe_camera(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get("http://localhost:8081/stream")
                return r.status_code in (200, 206)
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
            proc = await asyncio.create_subprocess_exec(
                "ps", "aux",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return sum(1 for line in out.decode().splitlines() if "ollama runner" in line)
        except Exception:
            return 0

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

    async def _check_all(self) -> None:
        self._issue_detected = False

        # 1. rover2-api RSS memory
        rss = await self._get_self_rss_mb()
        if rss is not None:
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

        # 2. CPU temperature
        temp = await self._get_temp_c()
        if temp is not None:
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

        # 3. hailo-ollama — 3 consecutive 500s triggers restart
        hailo_ok = await self._probe_hailo_ollama()
        if not hailo_ok:
            self._hailo_500_streak += 1
            if self._hailo_500_streak >= 3:
                self._issue_detected = True
                if self._can_act():
                    ok = await self._run_fix(["sudo", "systemctl", "restart", "hailo-ollama"])
                    result = "restart_failed"
                    if ok:
                        await asyncio.sleep(30.0)
                        recovered = await self._probe_hailo_ollama()
                        result = "recovered" if recovered else "still_down"
                        if recovered:
                            self._hailo_500_streak = 0
                    self._record_action()
                    self._last_action_desc = "hailo-ollama 500s → restart hailo-ollama"
                    self._log("ERROR", "hailo_ollama_unresponsive",
                              "restart_hailo_ollama", result)
        else:
            self._hailo_500_streak = 0

        # 4. rover-camera service
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

        # 5. MegaPi serial — handled by server.py megapi fault loop; skip here to avoid
        # double-action. Watchdog does not restart rover2-api (would kill itself).

        # 6. Ollama runner count > 1
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

        # 7. Disk space
        disk = await self._get_disk_pct()
        if disk is not None:
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
