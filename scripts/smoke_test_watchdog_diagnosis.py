#!/usr/bin/env python3
"""Post-deploy smoke test for the watchdog LLM diagnosis layer + self-test hook.

Modelled on scripts/smoke_test_emotion_state.py — reuses the same live WS
telemetry connection pattern and the same locally-imported canonical state
names (no duplication of "concerned"/"alert"/etc). Runs from central-computer
against the LIVE deployed Pi — no mocking.

Verifies:
  1. POST /api/watchdog/selftest raises a REAL alert through the deployed
     _emit_alert -> _sync_emotion_state path: the live WS telemetry
     `emotion_state` field transitions to concerned/alert within a few
     seconds of firing.
  2. It auto-clears back to a non-concerned/alert state within the self-test
     TTL (+ margin) — proving _clear_alert really ran, not just that the
     alert was raised once.
  3. GET /api/watchdog/log shows either a "diagnosis" line (the LLM produced
     a diagnosis for the synthetic alert) or a "diagnosis_skipped" line (the
     Hailo session was busy/unavailable) for the "selftest" key. Both are
     valid outcomes — the script states which occurred. Neither appearing at
     all is a failure (the diagnosis wiring never even tried).

Never touches motors or real services — /api/watchdog/selftest can only
raise the synthetic alert; it has no parameters selecting a real check.

Usage: python3 scripts/smoke_test_watchdog_diagnosis.py [host]
       (default host: 192.168.250.254)
Exit 0 = pass, 1 = fail.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pi"))
import emotion_state  # noqa: E402  local canonical VALID_STATES/name constants

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.250.254"
WS_URL = f"wss://{HOST}:8082/ws"
SELFTEST_URL = f"https://{HOST}:8082/api/watchdog/selftest"
LOG_URL = f"https://{HOST}:8082/api/watchdog/log"
STATUS_URL = f"https://{HOST}:8082/api/watchdog/status"

FIRE_AFTER_S = 1.0
SELFTEST_TTL_S = 8.0            # matches watchdog._SELFTEST_TTL_S
# Live telemetry cadence can lag well behind the nominal 400ms interval under
# real load, so the WS sample of "did it clear" is a best-effort observation,
# cross-checked against a deterministic direct REST read after the window.
COLLECT_S = FIRE_AFTER_S + SELFTEST_TTL_S + 10.0
DIAGNOSIS_WAIT_S = 25.0          # matches _run_diagnosis's own asyncio.wait_for timeout
DIAGNOSIS_POLL_INTERVAL_S = 3.0

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_FAIL = False


def _pass(msg: str) -> None:
    print(f"  OK   {msg}")


def _fail(msg: str) -> None:
    global _FAIL
    print(f"  FAIL {msg}")
    _FAIL = True


def _post_selftest() -> dict | None:
    req = urllib.request.Request(SELFTEST_URL, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  ... selftest POST error: {exc}")
        return None


def _get_log_lines() -> list[str]:
    req = urllib.request.Request(LOG_URL, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            return json.loads(resp.read()).get("lines", [])
    except Exception as exc:
        print(f"  ... log GET error: {exc}")
        return []


def _get_persistent_alerts() -> list[dict] | None:
    req = urllib.request.Request(STATUS_URL, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            return json.loads(resp.read()).get("persistent_alerts")
    except Exception as exc:
        print(f"  ... status GET error: {exc}")
        return None


async def main() -> int:
    try:
        import websockets
    except ImportError:
        _fail("the 'websockets' package is required on central-computer to run this smoke test")
        return 1

    print(f"==> ROVER2 watchdog diagnosis + self-test smoke test: {HOST}")
    print()

    samples: list[tuple[float, str | None]] = []
    selftest_result: dict | None = None
    try:
        async with websockets.connect(WS_URL, open_timeout=10, ssl=_ssl_ctx) as ws:
            _pass(f"WS connected: {WS_URL}")
            t0 = time.monotonic()
            fired = False
            fire_task: asyncio.Task | None = None
            while time.monotonic() - t0 < COLLECT_S:
                if not fired and time.monotonic() - t0 >= FIRE_AFTER_S:
                    fired = True
                    fire_task = asyncio.ensure_future(asyncio.to_thread(_post_selftest))
                    print(f"  ... fired POST /api/watchdog/selftest at t={time.monotonic() - t0:.1f}s")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "telemetry":
                    continue
                samples.append((round(time.monotonic() - t0, 2), d.get("emotion_state")))
            if fire_task is not None:
                try:
                    selftest_result = await asyncio.wait_for(fire_task, timeout=5)
                except asyncio.TimeoutError:
                    print("  ... selftest POST still in flight after 5s (ignored, non-fatal)")
    except Exception as exc:
        _fail(f"WS connection failed: {exc}")
        return 1

    if selftest_result is not None:
        _pass(f"self-test endpoint responded: {selftest_result}")
    else:
        _fail("self-test endpoint did not respond")

    if not samples:
        _fail("no telemetry messages received in the collection window")
        return 1
    _pass(f"received {len(samples)} telemetry messages over {COLLECT_S:.0f}s")

    invalid = [(t, v) for t, v in samples if v is not None and not emotion_state.is_valid(v)]
    if invalid:
        _fail(f"invalid emotion_state value(s) observed: {invalid}")
    else:
        _pass("all observed emotion_state values are valid")

    after_fire = [(t, v) for t, v in samples if t >= FIRE_AFTER_S]
    concerned_or_alert = [
        (t, v) for t, v in after_fire
        if v in (emotion_state.CONCERNED, emotion_state.ALERT)
    ]
    if concerned_or_alert:
        _pass(f"observed concerned/alert transition from the self-test: {concerned_or_alert[:3]}")
    else:
        _fail(
            "never observed concerned/alert after firing the self-test — "
            f"the face override may not be wired to the real alert path. samples: {after_fire}"
        )

    # Live telemetry cadence is best-effort (can lag well behind the nominal
    # 400ms interval under real load) so treat WS samples as supporting
    # evidence, and cross-check with a deterministic direct REST read — the
    # collection window already ran well past FIRE_AFTER_S + TTL by this point.
    ttl_deadline = FIRE_AFTER_S + SELFTEST_TTL_S
    after_ttl = [(t, v) for t, v in samples if t >= ttl_deadline + 1.0]
    ws_cleared = [(t, v) for t, v in after_ttl if v not in (emotion_state.CONCERNED, emotion_state.ALERT)]
    if ws_cleared:
        _pass(f"WS observed cleared state after the TTL: {ws_cleared[0]}")
    elif after_ttl:
        print(f"  ... WS samples after the TTL deadline still show concerned/alert: {after_ttl} "
              "(checking authoritative REST state next)")
    else:
        print(f"  ... no WS samples landed after the TTL deadline (t={ttl_deadline:.1f}s) — "
              "telemetry cadence was sparse; checking authoritative REST state next")

    persistent_alerts = _get_persistent_alerts()
    if persistent_alerts is None:
        _fail("could not read /api/watchdog/status to confirm the self-test alert cleared")
    elif any(a.get("metric") == "watchdog_selftest" for a in persistent_alerts):
        _fail(f"watchdog_selftest is STILL in persistent_alerts after its TTL: {persistent_alerts}")
    else:
        _pass("REST confirms watchdog_selftest is no longer in persistent_alerts (auto-clear worked)")

    print(f"  ... waiting up to {DIAGNOSIS_WAIT_S:.0f}s for a diagnosis record or a clean skip "
          "(polling GET /api/watchdog/log)")
    diagnosis_outcome: str | None = None
    deadline = time.monotonic() + DIAGNOSIS_WAIT_S
    while time.monotonic() < deadline and diagnosis_outcome is None:
        for line in _get_log_lines():
            if "selftest" not in line:
                continue
            if "diagnosis_skipped" in line:
                diagnosis_outcome = "skipped"
                break
            if "| diagnosis " in line:
                diagnosis_outcome = "ran"
                break
        if diagnosis_outcome is None:
            await asyncio.sleep(DIAGNOSIS_POLL_INTERVAL_S)

    if diagnosis_outcome == "ran":
        _pass("diagnosis RAN for the self-test alert (LLM produced a diagnosis record)")
    elif diagnosis_outcome == "skipped":
        _pass("diagnosis was CLEANLY SKIPPED for the self-test alert (Hailo session busy/unavailable)")
    else:
        _fail(
            "neither a diagnosis nor a clean skip was logged for the self-test alert within "
            f"{DIAGNOSIS_WAIT_S:.0f}s — the diagnosis wiring may not be reaching the deployed watchdog"
        )

    print()
    print("==> SMOKE TEST:", "FAIL" if _FAIL else "PASS")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
