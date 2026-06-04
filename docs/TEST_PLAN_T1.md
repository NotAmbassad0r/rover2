# T1 Follow Mode Test Plan

## Prerequisites
- Hailo HAT connected and `hailo_ready: true` in `/api/status`
- MegaPi connected (`/dev/ttyUSB0`)
- Rover on floor with ≥2m clear space ahead
- Camera active, body tracker warmed up (wait 30s after enabling DETECT/FOLLOW)
- Safety threshold: 40 cm (default)

---

## T1.3 — Person detected and tracked (centring) — ✓ PASS 2026-05-30

1. Enable CAMERA follow: `POST /api/tracking {"enabled":true,"detect_only":false}`
2. Stand 1m in front of rover, move left/right
3. **PASS**: rover turns to keep you centred in frame
4. **FAIL**: rover stays still or overshoots

---

## T1.4 — Advance toward distant person

1. Enable CAMERA follow
2. Stand 2m+ from rover (beyond `target_bbox_width: 0.35` — person looks small in frame)
3. **PASS**: rover advances toward you until you fill ~35% of frame width, then holds
4. **FAIL**: rover stays still or turns without advancing

---

## T1.5 — Hold position at correct distance

1. Enable CAMERA follow
2. Stand at ~1m from rover (bbox width ≈ 0.35)
3. **PASS**: rover holds position with minor corrections only; does not drift forward/back
4. **FAIL**: rover continues advancing or retreating continuously

---

## T1.6 — Obstacle avoidance while following

1. Enable CAMERA follow
2. Walk toward rover so ultrasonic reads <40 cm
3. **PASS**: rover stops (SAFETY BLOCKED); does not advance into you
4. **FAIL**: rover advances through the safety threshold

---

## T1.7 — BLE fallback on person lost (FUSED mode)

1. Enable FUSED follow: `POST /api/tracking {"enabled":true,"ble_follow_enabled":true}`
2. Confirm Z Flip 6 beacon seen: `GET /api/status` → `ble_seen: true`
3. Step out of camera view for 3+ seconds
4. **PASS**: rover activates BLE rotation mode (SOURCE shows "SEARCHING…"), turns toward beacon
5. **FAIL**: rover stops completely with no BLE response, or SOURCE shows "—"

---

## Test environment notes

- Turn speed: 210 (hard floor, may need to lower if surface is sticky)
- Forward speed: 170
- Centre zone: 30% — person must be outside this to trigger a turn
- Safe distance: 40 cm — ultrasonic must read <40 cm for T1.6 to trigger
- Hailo warmup: 30s after service start — wait before testing

## Logging results

Record results in `docs/TEST_RESULTS.md`:
```
| T1.4 | Advance | PASS/FAIL | notes |
| T1.5 | Hold    | PASS/FAIL | notes |
| T1.6 | Obstacle| PASS/FAIL | notes |
| T1.7 | BLE     | PASS/FAIL | notes |
```
