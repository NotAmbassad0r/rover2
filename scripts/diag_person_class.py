#!/usr/bin/env python3
"""Per-class person-detection diagnostic — RUNS ON THE PI.

DIAGNOSTIC ONLY: observe-and-report, no detection/pipeline behaviour changes.
Companion to scripts/diag_frame_timing.py (that one confirmed frames now flow
at a steady 1fps after the multipart-desync fix). This script answers a
different question: with a PERSON actually in frame, does the Hailo YOLOv8m
model ever emit a person-class score at all, and if it does, does it survive
body_tracker_parse.py's confidence+bounds+size filter?

Enables the existing, guarded [CLASSDIAG] instrumentation in
pi/body_tracker.py via a dedicated trigger file (/tmp/rover2_classdiag, same
established pattern as /tmp/rover2_pipediag, /tmp/rover2_framediag, and
/tmp/rover2_dump_nms) — off by default, logs one line per inference cycle
while active. Each line reports, straight from the already-computed raw NMS
tensor for that cycle:
  - the hardcoded person class id and the live confidence threshold in effect
  - person_raw_score: the person class's raw max score (score field, col 4),
    completely unfiltered — not even the empty-slot floor parse_best_person
    itself applies
  - raw_classes(score>0.05): every one of the 80 classes with any non-trivial
    raw score at all, sorted highest-first (top 16)
  - survives_filter: which class ids, if treated as the tracked target, would
    pass the EXACT SAME parse_best_person() confidence+bounds+size filter the
    real pipeline applies to the person class — computed by calling that same
    production function once per class id, not a reimplementation

Never restarts any service, never actuates motors. The only "control" action
is toggling DETECT-only tracking mode via the existing POST /api/tracking if
it isn't already active — restored afterward, same as diag_frame_timing.py.

Usage (on the Pi), with a person standing in the camera's view for the WHOLE
window (unlike diag_frame_timing.py, this one needs the person present
throughout, not just stepping in/out partway):

    python3 scripts/diag_person_class.py [window_seconds]
    (default window: 60s — at 1fps that's ~60 inference cycles, plenty to
    tell a real absence from a one-off miss)
"""
from __future__ import annotations

import ast
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request

API_BASE = "https://127.0.0.1:8082"
TRIGGER_PATH = "/tmp/rover2_classdiag"
WARMUP_TIMEOUT_S = 40.0

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_LINE_RE = re.compile(
    r"\[CLASSDIAG\] person_class=(?P<person_class>\d+) "
    r"confidence_threshold=(?P<conf>[\d.]+) "
    r"person_raw_score=(?P<praw>[\d.]+) "
    r"raw_classes\(score>[\d.]+\)=(?P<rawlist>\[.*?\]) "
    r"survives_filter=(?P<survives>\[.*\])\s*$"
)
_NONE_RE = re.compile(r"\[CLASSDIAG\] nms_output_to_tensor returned None")


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
    window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

    print("==> ROVER2 per-class person-detection diagnostic (DIAGNOSTIC ONLY — no behaviour changes)")
    print(f"    trigger: {TRIGGER_PATH}  window: {window_s:.0f}s  API: {API_BASE}")

    with open(TRIGGER_PATH, "w") as f:
        f.write("diag")
    print(f"  ... enabled [CLASSDIAG] verbose logging via {TRIGGER_PATH}")

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
    print("    STAND IN FRONT OF THE CAMERA FOR THE ENTIRE WINDOW — unlike the frame-timing "
          "diagnostic, this one needs a person present continuously, not just briefly, so we "
          "can tell a real absence from a one-off miss.")

    t0 = time.time()
    while time.time() - t0 < window_s:
        time.sleep(1.0)

    try:
        import os
        os.remove(TRIGGER_PATH)
        print(f"\n  ... removed {TRIGGER_PATH} ([CLASSDIAG] logging back off)")
    except OSError:
        pass
    if changed_tracking:
        print("  ... restoring tracking to OFF (as found before this diagnostic)")
        try:
            _post("/api/tracking", {"enabled": False, "detect_only": False})
        except Exception as exc:
            print(f"  ... WARNING: failed to restore tracking state: {exc}")

    lines = _journal_since("rover2-api", since)
    classdiag_lines = [l for l in lines if "[CLASSDIAG]" in l]

    print(f"\n{'=' * 70}\n[CLASSDIAG] per-cycle breakdown ({len(classdiag_lines)} cycles captured)\n{'=' * 70}")

    person_class: int | None = None
    confidence_threshold: float | None = None
    none_count = 0
    parsed_count = 0
    max_person_raw = 0.0
    person_ever_survived = False
    person_survived_scores: list[float] = []
    class_max_raw: dict[int, float] = {}
    class_survive_count: dict[int, int] = {}

    for line in classdiag_lines:
        tail = line.strip().split("body_tracker: ", 1)[-1]
        print(f"  {tail}")
        if _NONE_RE.search(line):
            none_count += 1
            continue
        m = _LINE_RE.search(line)
        if not m:
            continue
        parsed_count += 1
        person_class = int(m.group("person_class"))
        confidence_threshold = float(m.group("conf"))
        praw = float(m.group("praw"))
        max_person_raw = max(max_person_raw, praw)
        try:
            raw_list = ast.literal_eval(m.group("rawlist"))
        except (ValueError, SyntaxError):
            raw_list = []
        try:
            survives_list = ast.literal_eval(m.group("survives"))
        except (ValueError, SyntaxError):
            survives_list = []

        for cid, score in raw_list:
            class_max_raw[cid] = max(class_max_raw.get(cid, 0.0), float(score))
        for cid, score in survives_list:
            class_survive_count[cid] = class_survive_count.get(cid, 0) + 1
            if cid == person_class:
                person_ever_survived = True
                person_survived_scores.append(float(score))

    print(f"\n{'=' * 70}\nSUMMARY over {window_s:.0f}s window")
    print(f"  cycles captured: {len(classdiag_lines)}  parsed: {parsed_count}  "
          f"nms_output_to_tensor=None: {none_count}")
    if person_class is not None:
        print(f"  active person_class={person_class}  confidence_threshold={confidence_threshold:.2f}")
    print(f"  max person_raw_score observed across window: {max_person_raw:.3f}")
    print(f"  person_class ever survived full filter: {person_ever_survived}"
          + (f"  (scores: {person_survived_scores})" if person_survived_scores else ""))

    top_raw = sorted(class_max_raw.items(), key=lambda t: -t[1])[:10]
    print(f"  top classes by raw max score across window (class_id, max_score): {top_raw}")
    other_survivors = sorted(
        ((cid, n) for cid, n in class_survive_count.items() if cid != person_class),
        key=lambda t: -t[1],
    )[:10]
    print(f"  non-person classes that survived the full filter at least once "
          f"(class_id, cycles_survived): {other_survivors}")

    print(f"\n{'=' * 70}\nVERDICT")
    if parsed_count == 0:
        print("No parseable [CLASSDIAG] cycles captured — either the trigger never reached "
              "the running process, tracking wasn't actually active, or nms_output_to_tensor "
              "returned None every cycle (check the none_count above). Re-check and re-run.")
    elif person_ever_survived:
        print(
            f"PERSON PRESENT + KEPT: person IS detected in output — class {person_class} "
            f"survived the full confidence+bounds+size filter in at least one cycle "
            f"(scores seen: {person_survived_scores}). The gap, if the user still isn't being "
            f"followed/shown, is downstream of detection (rendering/follow logic), not "
            f"detection itself."
        )
    elif max_person_raw > confidence_threshold if confidence_threshold else False:
        print(
            f"PERSON IN RAW OUTPUT, FILTERED OUT: raw class {person_class} reached "
            f"{max_person_raw:.3f} (above the {confidence_threshold:.2f} confidence threshold) "
            f"at least once, but never survived the full filter — the rejection is in the "
            f"bounds/box-size checks, not the confidence threshold. Class-filter bug, not a "
            f"missing-class problem."
        )
    elif max_person_raw > 0.05:
        print(
            f"PERSON IN RAW OUTPUT, FILTERED OUT: raw class {person_class} present at up to "
            f"{max_person_raw:.3f} confidence but never reached the {confidence_threshold:.2f} "
            f"threshold in this window — kept set excludes it on confidence alone. "
            f"class-filter/threshold bug (or genuinely marginal detection quality)."
        )
    else:
        print(
            f"PERSON NEVER IN RAW OUTPUT: class {person_class} (the hardcoded person class) "
            f"never showed a meaningful raw score (max {max_person_raw:.3f}) despite a person "
            f"being in frame for the whole window. Other classes that DID fire: {top_raw} — "
            f"if any of those look like plausible 'always visible in this room' objects lighting "
            f"up instead, this points at either the wrong class index being used for 'person' on "
            f"this specific compiled .hef (i.e. class 0 isn't actually person on this model "
            f"export), or the wrong/non-person model being loaded entirely — not a missing "
            f"person in the model's true output. Cross-check top_raw's class ids against "
            f"whichever COCO/label mapping this .hef was actually exported with."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
