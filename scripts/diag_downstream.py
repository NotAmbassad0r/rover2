#!/usr/bin/env python3
"""Downstream-consumption diagnostic — RUNS ON THE PI.

DIAGNOSTIC ONLY: observe-and-report, no behaviour changes. Companion to
scripts/diag_person_class.py (that one proved person detection at the
Hailo/parse layer is correct — class 0, ~0.99 confidence, survives filtering
every cycle while a person stands in frame). This script answers what happens
to a CORRECT detection next: does it reach the outgoing WebSocket telemetry
payload (what /face and the main UI camera overlay both read), and does the
in-process follow-mode decision logic actually consume it.

Enables two independent, already-guarded [DOWNDIAG] taps via one shared
trigger file (/tmp/rover2_downdiag, same established pattern as
/tmp/rover2_classdiag, /tmp/rover2_framediag, /tmp/rover2_pipediag):
  - pi/ws_control.py's build_telemetry(): logs whether person_bbox/conf/
    follow_decision are present in the payload each telemetry tick, and a
    telemetry_ts monotonic timestamp.
  - pi/body_tracker.py's _process_frame(): logs what the follow-mode decision
    logic actually consumed (best/bbox/conf, person_tracked, detect_only,
    follow_mode) and what it decided, every inference cycle.

IMPORTANT SAFETY NOTE: this script deliberately keeps tracking in DETECT-ONLY
mode (same as every prior diagnostic in this series) — it does NOT enable
real FOLLOW mode, because that would drive the actual motors. This means the
follow-mode decision will legitimately always read "NONE
reason=detect_only_mode_never_drives" unless FOLLOW was already independently
enabled by a human before this script ran. That is expected, and is reported
as such rather than mistaken for a bug — verifying genuine drive-decision
behaviour would require deliberately enabling FOLLOW, which is a separate,
explicit, physical-motion action outside this diagnostic's scope.

Never restarts any service, never actuates motors, never enables FOLLOW. The
only "control" action is toggling DETECT-only tracking mode via the existing
POST /api/tracking if it isn't already active — restored afterward, same as
the prior diagnostics in this series.

Usage (on the Pi), with a person in the camera's view:

    python3 scripts/diag_downstream.py [window_seconds]
    (default window: 40s — the script prints timed prompts: stand still for
    the first half, then move side to side for the second half, so we can
    tell a correct-but-stale-looking overlay (1fps detection vs 0.4s
    telemetry cadence) from an actually-broken one)
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
TRIGGER_PATH = "/tmp/rover2_downdiag"
WARMUP_TIMEOUT_S = 40.0

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_TELEMETRY_RE = re.compile(
    r"\[DOWNDIAG\] telemetry_built person_detected=(?P<detected>\S+) "
    r"person_bbox=(?P<bbox>\[.*?\]|None) person_conf=(?P<conf>[\d.]+) "
    r"follow_decision=(?P<decision>\S+) telemetry_ts=(?P<ts>[\d.]+)"
)
_CONSUMED_RE = re.compile(
    r"\[DOWNDIAG\] consumed best=(?P<best>yes|no) bbox=(?P<bbox>\S.*?) conf=(?P<conf>[\d.]+) "
    r"person_tracked=(?P<tracked>\S+) detect_only=(?P<detect_only>\S+) follow_mode=(?P<mode>\S+)"
)
_DECISION_RE = re.compile(r"\[DOWNDIAG\] decision=(?P<decision>\S+)")


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
    window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    half = window_s / 2.0

    print("==> ROVER2 downstream-consumption diagnostic (DIAGNOSTIC ONLY — no behaviour changes)")
    print(f"    trigger: {TRIGGER_PATH}  window: {window_s:.0f}s  API: {API_BASE}")
    print("    SAFETY: tracking stays in DETECT-ONLY — FOLLOW is never enabled, motors never move.")

    with open(TRIGGER_PATH, "w") as f:
        f.write("diag")
    print(f"  ... enabled [DOWNDIAG] verbose logging via {TRIGGER_PATH}")

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
    print(f"    t=0s..{half:.0f}s   STAND STILL in frame.")
    t0 = time.time()
    while time.time() - t0 < half:
        time.sleep(1.0)
    print(f"    t={half:.0f}s..{window_s:.0f}s  NOW WALK SIDE TO SIDE within the camera's view.")
    while time.time() - t0 < window_s:
        time.sleep(1.0)

    try:
        os.remove(TRIGGER_PATH)
        print(f"\n  ... removed {TRIGGER_PATH} ([DOWNDIAG] logging back off)")
    except OSError:
        pass
    if changed_tracking:
        print("  ... restoring tracking to OFF (as found before this diagnostic)")
        try:
            _post("/api/tracking", {"enabled": False, "detect_only": False})
        except Exception as exc:
            print(f"  ... WARNING: failed to restore tracking state: {exc}")

    lines = _journal_since("rover2-api", since)
    downdiag_lines = [l for l in lines if "[DOWNDIAG]" in l]

    print(f"\n{'=' * 70}\n[DOWNDIAG] raw output ({len(downdiag_lines)} lines captured)\n{'=' * 70}")
    for line in downdiag_lines:
        print(f"  {line.strip().split('body_tracker: ', 1)[-1].split('ws_control: ', 1)[-1]}")

    telemetry_events = []
    for line in downdiag_lines:
        m = _TELEMETRY_RE.search(line)
        if m:
            telemetry_events.append(m)

    consumed_events = []
    decisions: dict[str, int] = {}
    for line in downdiag_lines:
        m = _CONSUMED_RE.search(line)
        if m:
            consumed_events.append(m)
        dm = _DECISION_RE.search(line)
        if dm:
            decisions[dm.group("decision")] = decisions.get(dm.group("decision"), 0) + 1

    telemetry_total = len(telemetry_events)
    telemetry_with_bbox = sum(1 for m in telemetry_events if m.group("bbox") != "None")

    # Quantify overlay staleness: how many consecutive telemetry ticks show the
    # EXACT SAME bbox before it changes — this is how many ~0.4s WS ticks a
    # human watching /face would see the box sit still even though (at 1fps)
    # a real, moving person has already moved on.
    run_lengths = []
    current_run = 0
    prev_bbox = object()  # sentinel, never equals a real string
    for m in telemetry_events:
        bbox = m.group("bbox")
        if bbox == prev_bbox:
            current_run += 1
        else:
            if current_run > 0:
                run_lengths.append(current_run)
            current_run = 1
            prev_bbox = bbox
    if current_run > 0:
        run_lengths.append(current_run)
    avg_run = sum(run_lengths) / len(run_lengths) if run_lengths else 0.0

    print(f"\n{'=' * 70}\nSUMMARY over {window_s:.0f}s window")
    print(f"  telemetry_built events: {telemetry_total}  (with non-null person_bbox: {telemetry_with_bbox})")
    print(f"  consumed (follow-mode input) events: {len(consumed_events)}")
    print(f"  decision tally: {decisions}")
    print(f"  avg consecutive telemetry ticks showing the SAME bbox before it changes: "
          f"{avg_run:.1f}  (~{avg_run * 0.4:.2f}s of visible staleness per detection, "
          f"at the default 0.4s telemetry interval)")

    print(f"\n{'=' * 70}\nVERDICT")
    if telemetry_total == 0:
        print("No [DOWNDIAG] telemetry_built events captured — the trigger likely never reached "
              "the running process, or no WebSocket client was connected during the window "
              "(build_telemetry() only runs per-connected-client). Re-check a client (browser/"
              "face page) is open and connected, and re-run.")
    elif telemetry_with_bbox == 0:
        print(
            "PERSON NOT IN TELEMETRY PAYLOAD: detection is correct upstream but never reached "
            "the outgoing WS payload as a non-null person_bbox in this window — telemetry "
            "propagation gap between body_tracker.get_state() and build_telemetry()."
        )
    else:
        print(
            f"PERSON REACHES TELEMETRY: {telemetry_with_bbox}/{telemetry_total} telemetry ticks "
            f"carried a real person_bbox this window — the same field face/index.html's "
            f"_ctrlDrawOverlay() and pi/web/static/index.html's drawDetectionOverlay() both read "
            f"directly (confirmed by static code trace: both draw ONLY from d.person_bbox/"
            f"person_conf, hardcode the literal label 'person', and neither can ever show a "
            f"non-person class — there is NO separate/different detection source feeding the "
            f"overlay). The earlier canned hypothesis 'overlay is fed by a different source' is "
            f"REFUTED by this trace: same detector, same fields, single path."
        )
        if decisions:
            non_none = {k: v for k, v in decisions.items() if not k.startswith("NONE")}
            if non_none:
                print(
                    f"  Follow-mode decisions observed (FOLLOW must already have been independently "
                    f"enabled): {non_none} — real movement decisions ARE being made from this same "
                    f"detection."
                )
            else:
                print(
                    "  Follow-mode decision every cycle: NONE, reason=detect_only_mode_never_drives "
                    "— this diagnostic deliberately never enables real FOLLOW (motors), so this is "
                    "expected, not a bug. pi/config.yaml has detect_only_on_start: true, so this is "
                    "also the robot's actual default boot state until a human explicitly enables "
                    "FOLLOW/CAMERA/FUSED mode."
                )
        if avg_run >= 2.0:
            print(
                f"\n  ADDITIONAL FINDING (not one of the canned buckets — reported because the data "
                f"supports it): the same detection value persists across an average of {avg_run:.1f} "
                f"consecutive telemetry ticks (~{avg_run * 0.4:.2f}s) before updating, consistent "
                f"with the detector's own 1fps cadence (frame_interval_s: 1.0 in config.yaml) being "
                f"slower than the 0.4s WS telemetry push. For a STATIONARY person this is invisible "
                f"(the box just sits correctly). For a MOVING person, the displayed box can visibly "
                f"lag up to ~1s behind their real position — long enough, at normal walking speed "
                f"within the camera's field of view, to visually sit over a neighbouring object "
                f"instead of the person. This is a strong candidate for the reported '/face shows "
                f"boxes on objects around me, not on me' symptom: not a wrong detection, not a "
                f"different source, but a display lag from the deliberate 1fps CPU-saving tradeoff "
                f"becoming visible only while the subject is moving."
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
