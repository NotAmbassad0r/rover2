#!/usr/bin/env python3
"""Per-frame-cycle timing diagnostic — RUNS ON THE PI.

DIAGNOSTIC ONLY: observe-and-report, no pipeline/watchdog/camera-service
behaviour changes. Companion to scripts/diag_detection_pipeline.py — that
script found decode/preprocess/inference/parse are all healthy when a frame
arrives, and that the fault is throughput at frame acquisition. This script
attributes WHERE that time actually goes: blocked on the camera socket read,
sleeping in body_tracker's own 1fps throttle, or something else.

Enables the existing, guarded [FRAMEDIAG] instrumentation in
pi/body_tracker.py via a dedicated trigger file (/tmp/rover2_framediag, same
established pattern as /tmp/rover2_pipediag and /tmp/rover2_dump_nms) — off
by default, logs one line per stream-loop cycle while active. Also polls the
camera's own /health endpoint during the window to see whether rover2-camera
is in its by-design 10fps-active or 1fps-idle mode throughout.

Never restarts any service, never actuates motors. The only "control" action
is toggling DETECT-only tracking mode via the existing POST /api/tracking if
it isn't already active — restored afterward, same as diag_detection_pipeline.py.

Usage (on the Pi):
    python3 scripts/diag_frame_timing.py [window_seconds]
    (default window: 120s — long enough to span several of the observed
    60-90s gaps; step a person in and out of frame partway through)
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request

API_BASE = "https://127.0.0.1:8082"
CAM_HEALTH_URL = "http://127.0.0.1:8081/health"
TRIGGER_PATH = "/tmp/rover2_framediag"
WARMUP_TIMEOUT_S = 40.0
CAM_POLL_INTERVAL_S = 10.0

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_CYCLE_RE = re.compile(
    r"\[FRAMEDIAG\] cycle kind=(?P<kind>\S+)"
    r"(?: read_wait=(?P<read_wait>[\d.]+)s)?"
    r"(?: sleep=(?P<sleep>[\d.]+)s)?"
    r"(?: decode=(?P<decode>[\d.]+)s)?"
    r"(?: process=(?P<process>[\d.]+)s)?"
    r"(?: gap_since_last_processed=(?P<gap>[\d.]+)s)?"
)


def _get(path: str) -> dict:
    req = urllib.request.Request(API_BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API_BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
        return json.loads(resp.read())


def _cam_health() -> dict | None:
    req = urllib.request.Request(CAM_HEALTH_URL, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  ... camera /health error: {exc}")
        return None


def _journal_since(service: str, since_iso: str) -> list[str]:
    try:
        out = subprocess.run(
            ["journalctl", "-u", service, "--since", since_iso, "--no-pager"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.splitlines()
    except Exception as exc:
        print(f"  ... journalctl error for {service}: {exc}")
        return []


def main() -> int:
    window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0

    print("==> ROVER2 per-frame-cycle timing diagnostic (DIAGNOSTIC ONLY — no behaviour changes)")
    print(f"    trigger: {TRIGGER_PATH}  window: {window_s:.0f}s  API: {API_BASE}")

    with open(TRIGGER_PATH, "w") as f:
        f.write("diag")
    print(f"  ... enabled [FRAMEDIAG] verbose logging via {TRIGGER_PATH}")

    status = _get("/api/status")
    was_enabled = bool(status.get("tracking_enabled"))
    was_detect_only = bool(status.get("tracking_detect_only"))
    changed_tracking = False
    if not (was_enabled or was_detect_only):
        print("  ... tracking currently off — enabling DETECT-only for this diagnostic (read-only, no driving)")
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

    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
    print(f"\n==> Running {window_s:.0f}s window now.")
    print("    Step a person INTO the camera's view partway through (e.g. around the "
          "midpoint) and OUT again before the window ends, so we can see whether "
          "motion/presence changes the camera's delivered rate.")

    cam_samples: list[dict] = []
    t0 = time.time()
    next_poll = t0
    while time.time() - t0 < window_s:
        now = time.time()
        if now >= next_poll:
            next_poll = now + CAM_POLL_INTERVAL_S
            cam = _cam_health()
            elapsed = now - t0
            if cam is not None:
                cam_samples.append({"t": round(elapsed, 1), **cam})
                print(f"  t={elapsed:5.1f}s  camera /health: {cam}")
        time.sleep(0.5)

    try:
        os.remove(TRIGGER_PATH)
        print(f"\n  ... removed {TRIGGER_PATH} ([FRAMEDIAG] logging back off)")
    except OSError:
        pass
    if changed_tracking:
        print("  ... restoring tracking to OFF (as found before this diagnostic)")
        try:
            _post("/api/tracking", {"enabled": False, "detect_only": False})
        except Exception as exc:
            print(f"  ... WARNING: failed to restore tracking state: {exc}")

    lines = [l for l in _journal_since("rover2-api", since) if "[FRAMEDIAG] cycle" in l]

    print(f"\n{'=' * 70}\n[FRAMEDIAG] per-cycle breakdown ({len(lines)} cycles captured)\n{'=' * 70}")

    kinds: dict[str, int] = {}
    total_read_wait = 0.0
    total_sleep = 0.0
    total_decode = 0.0
    total_process = 0.0
    gaps: list[float] = []

    for line in lines:
        m = _CYCLE_RE.search(line)
        if not m:
            continue
        print(f"  {line.strip().split('body_tracker: ', 1)[-1]}")
        kind = m.group("kind")
        kinds[kind] = kinds.get(kind, 0) + 1
        if m.group("read_wait"):
            total_read_wait += float(m.group("read_wait"))
        if m.group("sleep"):
            total_sleep += float(m.group("sleep"))
        if m.group("decode"):
            total_decode += float(m.group("decode"))
        if m.group("process"):
            total_process += float(m.group("process"))
        if m.group("gap"):
            gaps.append(float(m.group("gap")))

    print(f"\n{'=' * 70}\nSUMMARY over {window_s:.0f}s window")
    print(f"  cycle kinds: {kinds}")
    print(f"  total time blocked on socket read (read_wait, summed all cycles): {total_read_wait:.2f}s")
    print(f"  total time sleeping in the 1fps throttle (sleep, summed):         {total_sleep:.2f}s")
    print(f"  total decode time (summed, processed cycles only):               {total_decode:.3f}s")
    print(f"  total process time — resize+infer+parse (summed):                {total_process:.3f}s")
    if gaps:
        avg_gap = sum(gaps) / len(gaps)
        print(f"  processed frames: {len(gaps)}  avg inter-frame gap: {avg_gap:.2f}s  "
              f"min={min(gaps):.2f}s max={max(gaps):.2f}s")
    else:
        avg_gap = None
        print("  no consecutive processed frames observed in this window (fewer than 2 "
              "successful frames — gap cannot be computed)")

    print(f"\n{'=' * 70}\nVERDICT")
    if not lines:
        print("No [FRAMEDIAG] cycle lines captured at all in this window — either "
              "the trigger never reached the running process, or the stream loop "
              "isn't executing. Re-check the trigger file and tracking state.")
    else:
        read_dominant = total_read_wait > total_sleep and total_read_wait > (total_decode + total_process)
        sleep_dominant = total_sleep > total_read_wait and total_sleep > (total_decode + total_process)
        if read_dominant and avg_gap and avg_gap > 5.0:
            print(
                f"ROOT: blocked on socket read — {total_read_wait:.1f}s of this window's "
                f"{window_s:.0f}s was spent waiting inside req.read(4096) for camera bytes "
                f"(vs {total_sleep:.1f}s throttle-sleeping, {total_decode+total_process:.2f}s "
                f"processing). The camera feed is not delivering frames at the configured "
                f"1fps — check camera /health fps samples above against the delivered rate."
            )
        elif sleep_dominant and avg_gap and avg_gap <= 2.0:
            print(
                f"ROOT: body_tracker's own throttle accounts for most of the time "
                f"({total_sleep:.1f}s sleep vs {total_read_wait:.1f}s read_wait), and the "
                f"average inter-frame gap ({avg_gap:.2f}s) is close to the configured "
                f"frame_interval_s — this looks like NORMAL, by-design 1fps cadence, not a fault."
            )
        elif avg_gap and avg_gap > 5.0:
            print(
                f"ROOT: inconclusive between read_wait ({total_read_wait:.1f}s) and sleep "
                f"({total_sleep:.1f}s) — but the average inter-frame gap ({avg_gap:.2f}s) is "
                f"far above the configured 1fps interval regardless of which bucket dominates. "
                f"Compare against the camera /health fps samples above: if the camera itself "
                f"reports low fps throughout, the fault is upstream (camera production); if "
                f"the camera reports ~10fps (active) while body_tracker's read_wait stays "
                f"high, the fault is specifically in how body_tracker consumes the stream."
            )
        else:
            print(
                f"ROOT: camera/body_tracker read cadence looks close to the configured 1fps "
                f"in this window (avg gap={avg_gap if avg_gap else 'n/a'}) — did not reproduce "
                f"the 60-90s stall here. Re-run with a longer window or during/after a "
                f"rover2-camera restart to catch the degraded period."
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
