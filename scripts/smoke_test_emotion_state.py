#!/usr/bin/env python3
"""Post-deploy smoke test for the face emotion/state layer feature.

Runs from central-computer against the LIVE deployed Pi — no mocking, no
Hailo/camera/serial stubs. Verifies the runtime contract actually holds on the
deployed service, not just that it restarted:

  1. The real WS telemetry channel serialises `emotion_state` on every
     message, and every observed value is one of the six real names defined
     in pi/emotion_state.py (imported locally here as the canonical list —
     not duplicated, not queried remotely).
  2. Dispatching a real, read-only tool through the live agent
     (POST /api/agent/chat, "what is the temperature" -> get_diagnostics via
     the web-chat fast-path) drives an observable transition to
     acknowledging/working on that same live channel — proving
     RoverAgent._run_tool_with_reaction is actually wired into the deployed
     agent.py, not merely present in source.

Never actuates motors or restarts services — the fired tool is a read-only
diagnostics query.

Usage: python3 scripts/smoke_test_emotion_state.py [host]
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
CHAT_URL = f"https://{HOST}:8082/api/agent/chat"
COLLECT_S = 6.0
FIRE_AFTER_S = 1.0

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


def _post_chat(message: str) -> None:
    """Fire a real, read-only diagnostics query through the live agent."""
    body = json.dumps({"messages": [{"role": "user", "content": message}]}).encode()
    req = urllib.request.Request(
        CHAT_URL, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx) as resp:
            resp.read()
    except Exception as exc:
        print(f"  ... chat POST error (non-fatal to WS collection): {exc}")


async def main() -> int:
    try:
        import websockets
    except ImportError:
        _fail("the 'websockets' package is required on central-computer to run this smoke test")
        return 1

    print(f"==> ROVER2 emotion/state layer smoke test: {HOST}")
    print()

    samples: list[tuple[float, str | None]] = []
    try:
        async with websockets.connect(WS_URL, open_timeout=10, ssl=_ssl_ctx) as ws:
            _pass(f"WS connected: {WS_URL}")
            t0 = time.monotonic()
            fired = False
            fire_task: asyncio.Task | None = None
            while time.monotonic() - t0 < COLLECT_S:
                if not fired and time.monotonic() - t0 >= FIRE_AFTER_S:
                    fired = True
                    fire_task = asyncio.ensure_future(
                        asyncio.to_thread(_post_chat, "what is the temperature")
                    )
                    print(f"  ... fired real tool dispatch (get_diagnostics, read-only) "
                          f"at t={time.monotonic() - t0:.1f}s")
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
                    await asyncio.wait_for(fire_task, timeout=5)
                except asyncio.TimeoutError:
                    print("  ... chat POST still in flight after 5s (ignored, non-fatal)")
    except Exception as exc:
        _fail(f"WS connection failed: {exc}")
        return 1

    if not samples:
        _fail("no telemetry messages received in the collection window")
        return 1
    _pass(f"received {len(samples)} telemetry messages over {COLLECT_S}s")

    missing = [t for t, v in samples if v is None]
    if missing:
        _fail(f"{len(missing)}/{len(samples)} telemetry messages missing 'emotion_state' "
              f"(at t={missing})")
    else:
        _pass("every telemetry message includes 'emotion_state'")

    invalid = [(t, v) for t, v in samples if v is not None and not emotion_state.is_valid(v)]
    if invalid:
        _fail(f"invalid emotion_state value(s) observed: {invalid}")
    else:
        seen = sorted({v for _, v in samples if v is not None})
        _pass(f"all observed emotion_state values are valid: {seen}")

    after_fire = [(t, v) for t, v in samples if t >= FIRE_AFTER_S]
    transitioned = [
        (t, v) for t, v in after_fire
        if v in (emotion_state.ACKNOWLEDGING, emotion_state.WORKING)
    ]
    if transitioned:
        _pass(f"observed live tool-dispatch state transition: {transitioned}")
    else:
        _fail(
            "never observed 'acknowledging'/'working' after firing a real tool dispatch "
            f"— the feature may not actually be wired into the deployed agent.py. "
            f"samples after fire: {after_fire}"
        )

    print()
    print("==> SMOKE TEST:", "FAIL" if _FAIL else "PASS")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
