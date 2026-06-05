# ROVER2 test results log

Copy a section per session. Mark pass/fail and short notes.

---

## Session: 2026-05-16 — tethered (mains + eth)

**Tester:** ambassad0r  
**Pi:** eth <ROVER_ETH_IP> · WiFi <ROVER_WIFI_IP> available  
**Power:** mains (no untethered test1 until USB keep-alive)  
**Commit:** (local dev)

### Automated

- [x] `run_local_tests.sh`: 14 passed (safety skipped without pyserial on dev PC)
- [x] `check_rover_ready.sh`: PASSED — Hailo ready, serial OK, camera OK

### Manual (fill in after room tests)

### T1 Camera follow

| ID | Pass | Notes |
|----|------|-------|
| T1.1 | | |
| T1.2 | | |
| T1.3 | | |
| T1.4 | | |
| T1.5 | | |
| T1.6 | | |
| T1.7 | | |

### T2 Drive / arm / safety

| ID | Pass | Notes |
|----|------|-------|
| T2.1 | | |
| T2.2 | | |
| T2.3 | | |
| T2.4 | | |
| T2.5 | | |
| T2.6 | | |

### T3 BLE (stationary)

| ID | Pass | Notes |
|----|------|-------|
| T3.1 | | |
| T3.2 | | |
| T3.3 | | |

### Summary

- **Today sign-off (T4):** pass / fail / partial  
- **Follow-ups:**

---

## Session: 2026-06-05 — untethered battery (U0 + M1)

**Tester:** ambassad0r  
**Pi:** WiFi only (no eth tether)  
**Power:** Viking PN-964PD + 5A e-marked USB-C cable (100W rated)  
**Commit:** dev branch, post body_tracker CPU optimisation

### U0 — Untethered battery test

| Item | Result |
|------|--------|
| Power bank | Viking PN-964PD |
| Cable | 5A e-marked USB-C (100W rated) — replaces previous 3A cable (was 15W ceiling) |
| Boot on battery | **PASS** — Pi 5 + Hailo boot without cutoff |
| Sustained inference | **PASS** — detect-only at 1fps ran for full session without EXT5V droop or PMIC cutoff |
| CPU (detect-only) | ~16–22% (post optimisation: TurboJPEG 320×240, 1fps, stream-skip sleep) |
| Idle CPU | ~0–2% |

**Status: PASS 2026-06-05**

### M1 — Follow mobility test

| Item | Result |
|------|--------|
| Mode | Untethered WiFi + battery |
| Sequence | Boot → stabilise → FOLLOW enabled → walked room → robot tracked and drove |
| Person tracking | **PASS** — robot followed operator across room without loss of lock |
| Motor cutoff | **PASS** — no cutoff during sustained follow on battery |
| Tether | None (fully untethered) |
| Mains | None |

**Status: PASS 2026-06-05**

### Summary

- **U0:** PASS — Pi 5 + Hailo sustained on Viking bank with 5A cable
- **M1:** PASS — untethered WiFi + battery follow verified end-to-end
- **v1.0.0 milestone:** met — merge dev→main, tag v1.0.0
