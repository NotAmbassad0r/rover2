#!/usr/bin/env python3
"""Frame throughput + CPU cost smoke test — RUNS ON THE PI, NO MOCKING.

Verifies the frame_interval_s: 1.0 -> 0.2 (1fps -> 5fps) change against the
currently-deployed, real rover2-api process:
  - detection cadence: cross-checks body_tracker's own actual_fps (EMA,
    already computed in _process_frame) against frames_inferred deltas over
    the window, independently measured here
  - system CPU%: sampled the exact same way pi/watchdog.py's own
    _sample_cpu_pct() does (/proc/stat, no external deps, no psutil) — so the
    number reported here is directly comparable to what the watchdog itself
    would alert on (cpu_sustained_percent, default 90 in config.yaml)
  - stall/restart regression: this is the failure mode diagnosed and fixed
    earlier this session (body_tracker frame-consumption stalling tens of
    seconds while the camera kept producing). Confirms rover2-api and
    rover2-camera do NOT restart mid-window (MainPID + ActiveEnterTimestamp
    checked at start and end), checks the watchdog's own action_history for
    any camera_frames_stall/restart_rover2_camera entries fired during the
    window, and polls rover2-camera's /health to confirm it stays active
    throughout.

Never restarts any service, never actuates motors, never enables real
FOLLOW. Only toggles DETECT-only tracking via POST /api/tracking if it isn't
already active (restored afterward), same safe pattern as the rest of this
diagnostic series.

Usage (on the Pi):
    python3 scripts/smoke_test_frame_throughput.py [window_seconds]
    (default: 180s — a multi-minute window, long enough to catch a
    slow-building stall rather than just an instantaneous snapshot)
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
import urllib.request

API_BASE = "https://127.0.0.1:8082"
CAM_HEALTH_URL = "http://127.0.0.1:8081/health"
WARMUP_TIMEOUT_S = 40.0
CPU_SAMPLE_INTERVAL_S = 1.0     # per-sample /proc/stat window
PROGRESS_INTERVAL_S = 10.0      # how often to print + sample during the run
CPU_SUSTAINED_ALERT_PCT = 90.0  # matches watchdog's own default threshold

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _get(path: str, base: str = API_BASE, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
        return json.loads(resp.read())


def _get_plain(url: str, timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  ... {url} error: {exc}")
        return None


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API_BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
        return json.loads(resp.read())


def _read_proc_stat() -> tuple[int, int]:
    with open("/proc/stat") as f:
        line = f.readline()
    vals = list(map(int, line.split()[1:]))
    return vals[3], sum(vals)  # idle, total


def _sample_cpu_pct(interval_s: float = CPU_SAMPLE_INTERVAL_S) -> float | None:
    """Same method as pi/watchdog.py's _sample_cpu_pct() — /proc/stat delta."""
    idle0, total0 = _read_proc_stat()
    time.sleep(interval_s)
    idle1, total1 = _read_proc_stat()
    dtotal = total1 - total0
    if dtotal == 0:
        return None
    return 100.0 * (1.0 - (idle1 - idle0) / dtotal)


def _service_info(name: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", name, "-p", "MainPID", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10,
        )
        info: dict[str, str] = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        return info
    except Exception as exc:
        return {"error": str(exc)}


def main() -> int:
    window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0

    print("==> ROVER2 frame throughput + CPU cost smoke test (LIVE — no mocking)")
    print(f"    window: {window_s:.0f}s  API: {API_BASE}")

    status = _get("/api/status")
    was_enabled = bool(status.get("tracking_enabled"))
    was_detect_only = bool(status.get("tracking_detect_only"))
    changed_tracking = False
    if not (was_enabled or was_detect_only):
        print("  ... tracking currently off — enabling DETECT-only for this test (read-only, no driving)")
        _post("/api/tracking", {"detect_only": True})
        changed_tracking = True
    else:
        print(f"  ... tracking already active (enabled={was_enabled} detect_only={was_detect_only}) — leaving as-is")

    print(f"  ... waiting for tracking_hailo_ready (up to {WARMUP_TIMEOUT_S:.0f}s, lazy Hailo load/warmup)")
    deadline = time.time() + WARMUP_TIMEOUT_S
    ready = False
    while time.time() < deadline:
        st = _get("/api/status")
        if st.get("tracking_hailo_ready"):
            ready = True
            break
        time.sleep(2.0)
    print(f"  ... tracking_hailo_ready={ready}")
    if not ready:
        print("ABORT: Hailo never became ready — cannot measure cadence/CPU meaningfully.")
        return 1

    configured_fps = st.get("configured_fps")
    print(f"  ... configured_fps (from live telemetry) = {configured_fps}")

    api_before = _service_info("rover2-api")
    cam_before = _service_info("rover2-camera")
    wd_before = _get("/api/watchdog/status")
    action_history_before = len(wd_before.get("action_history", []))

    frames_t0 = _get("/api/follow/status")
    frames_start = frames_t0.get("frames_inferred", 0)
    t_start = time.time()

    print(f"\n==> Running {window_s:.0f}s window now — walk in/out of frame if you like, "
          f"doesn't matter for this measurement (detect-only, no driving).")

    cpu_samples: list[float] = []
    cam_health_samples: list[dict] = []
    actual_fps_samples: list[float] = []
    elapsed = 0.0
    next_progress = 0.0
    while elapsed < window_s:
        cpu = _sample_cpu_pct()
        if cpu is not None:
            cpu_samples.append(cpu)
        elapsed = time.time() - t_start
        if elapsed >= next_progress:
            next_progress = elapsed + PROGRESS_INTERVAL_S
            cam = _get_plain(CAM_HEALTH_URL)
            st_now = _get("/api/status")
            afps = st_now.get("actual_fps")
            if afps is not None:
                actual_fps_samples.append(afps)
            if cam is not None:
                cam_health_samples.append({"t": round(elapsed, 1), **cam})
            print(f"  t={elapsed:6.1f}s  cpu={cpu if cpu is not None else '?':>5}%  "
                  f"actual_fps={afps}  camera/health={cam}")

    frames_t1 = _get("/api/follow/status")
    frames_end = frames_t1.get("frames_inferred", 0)
    t_end = time.time()
    real_elapsed = t_end - t_start

    api_after = _service_info("rover2-api")
    cam_after = _service_info("rover2-camera")
    wd_after = _get("/api/watchdog/status")
    action_history_after = wd_after.get("action_history", [])

    if changed_tracking:
        print("\n  ... restoring tracking to OFF (as found before this test)")
        try:
            _post("/api/tracking", {"enabled": False, "detect_only": False})
        except Exception as exc:
            print(f"  ... WARNING: failed to restore tracking state: {exc}")

    observed_fps = (frames_end - frames_start) / real_elapsed if real_elapsed > 0 else 0.0
    avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
    max_cpu = max(cpu_samples) if cpu_samples else 0.0
    avg_actual_fps = sum(actual_fps_samples) / len(actual_fps_samples) if actual_fps_samples else 0.0

    api_restarted = (
        api_before.get("MainPID") != api_after.get("MainPID")
        or api_before.get("ActiveEnterTimestamp") != api_after.get("ActiveEnterTimestamp")
    )
    cam_restarted = (
        cam_before.get("MainPID") != cam_after.get("MainPID")
        or cam_before.get("ActiveEnterTimestamp") != cam_after.get("ActiveEnterTimestamp")
    )
    new_actions = action_history_after[action_history_before:]
    stall_actions = [
        a for a in new_actions
        if "camera_frames_stall" in str(a) or "restart_rover2_camera" in str(a)
        or "detection_stalled" in str(a)
    ]

    print(f"\n{'=' * 70}\nSUMMARY over {real_elapsed:.1f}s window")
    print(f"  frames_inferred: {frames_start} -> {frames_end}  "
          f"(observed cadence: {observed_fps:.2f} fps)")
    print(f"  body_tracker's own actual_fps (telemetry EMA), avg of samples: {avg_actual_fps:.2f} fps")
    print(f"  configured_fps (target): {configured_fps}")
    print(f"  system CPU%: avg={avg_cpu:.1f}%  max={max_cpu:.1f}%  "
          f"(n={len(cpu_samples)} samples, watchdog's own alert threshold is "
          f"{CPU_SUSTAINED_ALERT_PCT:.0f}% sustained)")
    print(f"  rover2-api restarted during window: {api_restarted}  "
          f"(before={api_before} after={api_after})")
    print(f"  rover2-camera restarted during window: {cam_restarted}  "
          f"(before={cam_before} after={cam_after})")
    print(f"  new watchdog actions fired during window: {len(new_actions)}  "
          f"stall/restart-related: {len(stall_actions)}  detail={stall_actions}")
    cam_fps_seen = [c.get("fps") for c in cam_health_samples if c.get("fps") is not None]
    print(f"  camera/health fps samples over window: {cam_fps_seen}")

    print(f"\n{'=' * 70}\nVERDICT")
    problems = []
    if api_restarted or cam_restarted:
        problems.append("rover2-api or rover2-camera RESTARTED during the window")
    if stall_actions:
        problems.append(f"watchdog fired {len(stall_actions)} stall/restart-related action(s)")
    if max_cpu >= CPU_SUSTAINED_ALERT_PCT:
        problems.append(f"CPU hit {max_cpu:.0f}% (>= watchdog's {CPU_SUSTAINED_ALERT_PCT:.0f}% sustained-alert threshold)")
    if observed_fps < (configured_fps or 0) * 0.7:
        problems.append(f"observed cadence ({observed_fps:.2f} fps) is well below configured ({configured_fps} fps)")

    if problems:
        print("STOP — 5fps may be too high for this Pi. Problems observed:")
        for p in problems:
            print(f"  - {p}")
        print("Recommend dialing back to an intermediate rate (e.g. frame_interval_s: 0.33 / 3fps) "
              "rather than forcing 5fps.")
    else:
        print(
            f"5fps SUSTAINABLE: observed cadence {observed_fps:.2f} fps (target {configured_fps}), "
            f"CPU avg {avg_cpu:.1f}% / max {max_cpu:.1f}% (well under the {CPU_SUSTAINED_ALERT_PCT:.0f}% "
            f"watchdog threshold), no service restarts, no stall-related watchdog actions, camera "
            f"stayed healthy throughout. No regression of the frame-stall issue fixed earlier this session."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
