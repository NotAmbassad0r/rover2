# ROVER2 — project definition

Minimal companion robot stack: **MegaPi motion**, **ultrasonic safety**, **web control**, **Hailo person follow**, **BLE beacon fallback**. Runs **alongside** ROVER v1 on port **8082**; only one stack may use `/dev/ttyUSB0` at a time.

**Session source of truth:** `HANDOFF.md` (operational detail). This file is scope and phase map.

---

## Goals (this repo)

1. Drive chassis from browser (hold-to-move, release-to-stop).
2. Open/close gripper; arm lift on PORT3B.
3. Ultrasonic distance + forward safety gate.
4. Camera person follow (Hailo YOLOv8m) with BLE RSSI fallback.
5. Obstacle steer during follow (not freeze at walls).
6. Web UI: CONTROL + TOOLS (diagnostics, scripts, logs, config).
7. Single deploy path: `./deploy_pi.sh` → `/opt/rover2/`.

---

## Out of scope (stay in ROVER v1 / backlog)

- Android brain (A32 / Flip 6 apps), voice, LLM, agent loop, world state
- Extension plugin platform, Home Assistant, patrol
- Wear OS watch, security hardening (WSS, API tokens) — unless explicitly added later
- pylgbst head pan/tilt, multi-model HAT pipeline, semantic memory

See `~/Documents/projects/rover/ROVER_BACKLOG.md` for the full v1 wishlist.  
See `docs/ROADMAP_UNTIL_HARDWARE.md` for what ROVER2 should do **before** USB keep-alive hardware.

---

## Hardware (shared with v1)

| Component | Role |
|-----------|------|
| Raspberry Pi 5 8GB + AI HAT+ 2 | Host API + Hailo inference |
| Makeblock MegaPi | Motors, gripper, ultrasonic |
| Ultimate 2.0 chassis | PORT1B / PORT2B drive |
| Flip 6 (optional) | BLE beacon (`fcf1` UUID) for follow fallback |
| Viking PN-964PD | Battery (untethered test1 after USB keep-alive module) |

### Network

| Interface | IP |
|-----------|-----|
| eth0 (deploy / lab) | **192.168.70.11** |
| WiFi (GUCZ-744) | 192.168.250.254 |
| Tailscale | 100.67.13.10 |

Dev machine (`192.168.20.11`) has **no route** to Pi WiFi — deploy via eth0.

---

## Software architecture

```
Browser  http://<pi>:8082/
    → WebSocket /ws          drive, grip, arm, tracking, telemetry
    → REST /api/*            status, diagnostics, logs, config, maintenance
    → Camera (preview)       http://<pi>:8081/stream  (rover-camera.service)
        → main.py → server.py, ws_control.py, safety.py, diagnostics.py
        → body_tracker.py    Hailo follow + BLE fallback + obstacle steer
        → ble_tracker.py, megapi.py → /dev/ttyUSB0
```

---

## Phase roadmap (ROVER2)

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **0** | Motors, gripper, ultrasonic, web D-pad | **Done** |
| **1** | Forward obstacle stop (< safe distance) | **Done** |
| **2** | WebSocket D-pad, telemetry, stop on disconnect | **Done** |
| **2b** | Arm lift (PORT3B), firmware 1.0.3 | **Done** |
| **3** | Hailo camera person follow | **Done** (bench verified) |
| **4** | BLE beacon fallback follow | **Done** (bench verified) |
| **5** | Web TOOLS: diagnostics, scripts, logs, config | **Done** |
| **test1** | Follow mobility (M1 — pass-through OK) | **In progress** |
| **v1.0.0** | M1 pass → tag release | Pending |

---

## ROVER2 vs v1 backlog (what to do next)

| Priority | ROVER2 action | v1 backlog analogue |
|----------|---------------|---------------------|
| **P0** | Complete `docs/TEST_PLAN.md` **T0–T4** (tethered) | Phase 12 integration tests (subset) |
| **P0** | Log results in `docs/TEST_RESULTS.md` | — |
| **P1** | Tune `pi/config.yaml` from field tests | Follow distance PID, camera latency (later) |
| **P2** | `usb_max_current_enable=1` on Pi before battery | v1 power backlog |
| **Optional** | **U0–U2** pure battery hardening | After keep-alive dongle |
| **Gate** | **v1.0.0** | **M1** mobility pass (`docs/TEST_PLAN.md`) |
| **Later** | YOLOv8 **pose** model, 2nd ultrasonic, MegaPi `get_vin()` | v1 HAT / hardware backlog |
| **Never here** | Android, LLM, extensions, watch, WSS (unless new charter) | Most of `ROVER_BACKLOG.md` |

---

## Paths

| Machine | Path |
|---------|------|
| Dev | `~/Documents/projects/rover2/` |
| Pi | `/opt/rover2/` |
| v1 (reference) | `~/Documents/projects/rover/` → `/opt/rover/` |

## Relation to ROVER v1

v1 is the full system (port **8080**). ROVER2 reuses wiring and a compatible serial protocol; code stays small and testable. Do not merge repos without a deliberate decision.
