#!/usr/bin/env python3
"""Standalone detection-pipeline diagnostic runner — RUNS ON THE PI.

DIAGNOSTIC ONLY: observe-and-report, no pipeline behaviour changes. Enables
the existing, guarded [PIPEDIAG] instrumentation in pi/body_tracker.py and
pi/camera_server.py via the same trigger-file pattern the NMS dump tap
already uses (presence of /tmp/rover2_pipediag), exercises the
ALREADY-RUNNING rover2-api body_tracker for two short windows — an empty
scene and a person-present scene — and reports, per pipeline stage, whether
that stage produced non-empty output in each scene. Prints a single verdict
line naming the first stage that produced nothing when a person was present.

Never restarts any service, never actuates motors. The only "control" action
is toggling DETECT-only tracking mode via the existing POST /api/tracking
(read-only inference, no driving) if tracking isn't already active — and it
restores whatever state it found beforehand.

Usage (on the Pi):
    python3 scripts/diag_detection_pipeline.py
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

API_BASE = "https://127.0.0.1:8082"
CAM_HEALTH_URL = "http://127.0.0.1:8081/health"
TRIGGER_PATH = "/tmp/rover2_pipediag"
WINDOW_S = 6.0
COUNTDOWN_S = 4
WARMUP_TIMEOUT_S = 40.0

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# (stage key, systemd unit that logs it, substring tag to grep for)
STAGES = [
    ("a_camera", "rover2-camera", "[PIPEDIAG] camera:"),
    ("b_frame_acquisition", "rover2-api", "[PIPEDIAG] b_frame_acquisition:"),
    ("c_turbojpeg_decode", "rover2-api", "[PIPEDIAG] c_turbojpeg_decode:"),
    ("d_preprocess", "rover2-api", "[PIPEDIAG] d_preprocess:"),
    ("e_hailo_inference", "rover2-api", "[PIPEDIAG] e_hailo_inference"),  # matches per-frame + _init
    ("f_raw_nms_to_parser", "rover2-api", "[PIPEDIAG] f_raw_nms_to_parser:"),
]


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
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.splitlines()
    except Exception as exc:
        print(f"  ... journalctl error for {service}: {exc}")
        return []


def _countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  capturing in {remaining}...", flush=True)
        time.sleep(1)
    print("  capturing now.", flush=True)


def _capture_scene(label: str) -> dict[str, list[str]]:
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
    _countdown(COUNTDOWN_S)
    time.sleep(WINDOW_S)
    result: dict[str, list[str]] = {}
    print(f"\n[PIPEDIAG] --- {label} scene, {WINDOW_S:.0f}s window ---")
    for key, service, tag in STAGES:
        lines = [l for l in _journal_since(service, since) if tag in l]
        result[key] = lines
        status = "non-empty" if lines else "EMPTY"
        print(f"  [{label}] {key:22s} -> {status} ({len(lines)} matching lines)")
        for l in lines[:3]:
            print(f"      {l.strip()}")
    return result


def main() -> int:
    print("==> ROVER2 detection pipeline diagnostic (DIAGNOSTIC ONLY — no pipeline changes)")
    print(f"    trigger: {TRIGGER_PATH}  window: {WINDOW_S:.0f}s  API: {API_BASE}")

    with open(TRIGGER_PATH, "w") as f:
        f.write("diag")
    print(f"  ... enabled [PIPEDIAG] verbose logging via {TRIGGER_PATH}")

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
    if not ready:
        print("  WARNING: Hailo never reported ready — results below may reflect warmup, not the real symptom.")

    empty_result: dict[str, list[str]] = {}
    person_result: dict[str, list[str]] = {}
    try:
        cam = _cam_health()
        print(f"  camera /health: {cam}")

        print("\n" + "=" * 70)
        input("Ensure NO person is in the camera's view, then press Enter to capture the EMPTY scene...")
        empty_result = _capture_scene("empty")

        print("\n" + "=" * 70)
        input("Now step INTO the camera's view, then press Enter to capture the PERSON-PRESENT scene...")
        person_result = _capture_scene("person")
    finally:
        try:
            os.remove(TRIGGER_PATH)
            print(f"\n  ... removed {TRIGGER_PATH} (PIPEDIAG logging back off)")
        except OSError:
            pass
        if changed_tracking:
            print("  ... restoring tracking to OFF (as found before this diagnostic)")
            try:
                _post("/api/tracking", {"enabled": False, "detect_only": False})
            except Exception as exc:
                print(f"  ... WARNING: failed to restore tracking state: {exc}")

    print("\n" + "=" * 70)
    print("VERDICT ANALYSIS")
    order = [s[0] for s in STAGES]
    first_dead = None
    ok_before: list[str] = []
    for key in order:
        if person_result.get(key):
            ok_before.append(key)
            continue
        first_dead = key
        break

    if first_dead is None:
        print(
            "All instrumented stages show output in the person-present scene — the "
            "signal is NOT dying before the parser at any of stages a-f. Re-examine "
            "body_tracker_parse.py's own logic instead (out of scope for this diagnostic)."
        )
    else:
        desc = ", ".join(ok_before) if ok_before else "(none — even camera/frame acquisition is empty)"
        print(f"FIRST DEAD STAGE: {first_dead} — stages OK before it: {desc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
