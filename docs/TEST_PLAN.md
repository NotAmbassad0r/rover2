# ROVER2 test plan

**Power context:** You can run full follow tests **without** the USB keep-alive dongle using any of:

1. **Mains + eth** (lab) — `<ROVER_ETH_IP>`
2. **Viking pass-through** — charger → bank IN, Pi → bank OUT (walk with long USB-C)
3. **Battery + virtual dongle** — `rover2-virtual-usb-dongle.service` (less reliable; OK for short walks)

**Optional upgrade:** dedicated USB keep-alive in spare port improves untethered **U0** idle time; not required for **M1** mobility below.

**Automation:** `./scripts/run_local_tests.sh` (dev PC) · `./scripts/check_rover_ready.sh [host]` (Pi preflight)

Record manual results in `docs/TEST_RESULTS.md`.

---

## Today — tethered (eth `<ROVER_ETH_IP>`)

### T0 — Preflight (automated + 2 min manual)

| ID | Test | How | Pass |
|----|------|-----|------|
| T0.1 | Unit tests on dev PC | `./scripts/run_local_tests.sh` | All OK |
| T0.2 | Pi services | `./scripts/check_rover_ready.sh` | All OK |
| T0.3 | Web UI loads | Browser `http://<ROVER_ETH_IP>:8082/` Ctrl+Shift+R | Page + camera preview |
| T0.4 | Serial stable | STATUS: SERIAL ok; no rapid disconnect in UI | Stable ≥2 min |

### T1 — Camera follow (Phase 3)

| ID | Test | Steps | Pass |
|----|------|-------|------|
| T1.1 | Hailo available | STATUS: FOLLOW available, Hailo ready | Yes |
| T1.2 | Person detect | DETECT ON, walk in frame | PERSON on/off |
| T1.3 | Centre hold | FOLLOW ON, stand centred, close | HOLD / little motion |
| T1.4 | Turn left | Move left of frame | LEFT turn |
| T1.5 | Turn right | Move right of frame | RIGHT turn |
| T1.6 | Advance | Step back (smaller bbox) | FWD |
| T1.7 | Obstacle steer | FOLLOW ON, wall &lt;40 cm ahead | Turns, no ram |

### T2 — Drive / arm / safety

| ID | Test | Steps | Pass |
|----|------|-------|------|
| T2.1 | D-pad | FOLLOW OFF, each direction | Moves correct way |
| T2.2 | Stop | Release / stop | Motors stop |
| T2.3 | Gripper | Open / close | Works |
| T2.4 | Arm | Hold up/down, nudge | PORT3B moves |
| T2.5 | Ultrasonic | STATUS shows cm; changes near wall | Plausible |
| T2.6 | Forward block | D-pad FWD into wall &lt;40 cm | Blocked / stops |

### T3 — BLE fallback (stationary, tethered)

| ID | Test | Steps | Pass |
|----|------|-------|------|
| T3.1 | Beacon seen | Flip 6 / fcf1 nearby | BLE SEEN + dBm |
| T3.2 | Handoff | FOLLOW ON, cover lens &gt;2 s | BLE FOLLOWING, rotate |
| T3.3 | Reacquire | Uncover lens | Camera follow resumes |

### T4 — Sign-off today

| ID | Criterion | Pass |
|----|-----------|------|
| T4.1 | T1.1–T1.6 pass | ☐ |
| T4.2 | T2.1–T2.6 pass | ☐ |
| T4.3 | T3.1–T3.3 pass (or N/A if no beacon) | ☐ |
| T4.4 | Notes in `docs/TEST_RESULTS.md` | ☐ |

---

## M1 — Mobility (no keep-alive dongle required)

Use **pass-through** or long tether; phone on WiFi optional if eth cable follows rover.

| ID | Test | Pass |
|----|------|------|
| M1.1 | FOLLOW ON, walk room on pass-through / long cable | ☐ |
| M1.2 | Leave frame → BLE ≥30 s → return | ☐ |
| M1.3 | Doorway steer (no freeze) | ☐ |
| M1.4 | Tune in web **TOOLS → FOLLOW TUNING** if needed | ☐ |

If M1.1–M1.3 pass → treat as **test1 functional pass** for v1.0.0 (even before pure-battery U0).

---

## Upcoming — pure battery (optional hardening)

### U0 — Power & network

| ID | Test | Pass |
|----|------|------|
| U0.1 | Keep-alive in spare Viking port; double-tap wake | Bank stays on ≥30 min idle |
| U0.2 | Unplug eth; Pi on WiFi `<ROVER_WIFI_IP>` | Phone reaches UI |
| U0.3 | Optional: disable `rover2-virtual-usb-dongle` if hardware enough | Pi stable |

### U1 — test1 mobility

| ID | Test | Pass |
|----|------|------|
| U1.1 | Phone `http://<ROVER_WIFI_IP>:8082/`, FOLLOW ON | ☐ |
| U1.2 | Walk apartment, stay in frame | Camera follow ≥10 min |
| U1.3 | Leave frame | BLE follow ≥30 s |
| U1.4 | Return to frame | Camera resumes |
| U1.5 | Doorway / wall | Steer around, no freeze |

### U2 — Release

| ID | Test | Pass |
|----|------|------|
| U2.1 | All U0–U1 pass | ☐ |
| U2.2 | Tag `v1.0.0`, merge dev→main (HANDOFF workflow) | ☐ |

---

## Regression (any deploy)

```bash
./scripts/run_local_tests.sh
./deploy_pi.sh
./scripts/check_rover_ready.sh <ROVER_ETH_IP>
```

Manual smoke: T1.2, T2.1, T3.1 (5 min).

---

## Tuning reference (if T1 fails)

**Web:** TOOLS → **FOLLOW TUNING** → SAVE (writes `config.yaml` + applies live).

**File:** `pi/config.yaml` → `body_tracker`, `safety`, `ble_tracker` RSSI speeds.
