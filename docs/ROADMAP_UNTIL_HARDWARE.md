# ROVER2 — software roadmap (hardware not a blocker)

Progress does **not** require the USB keep-alive module. Use mains, pass-through, or virtual dongle for mobility tests.

## Done

| Item | Where |
|------|--------|
| Diagnostics + scripts API + TOOLS tab | `pi/diagnostics.py`, web UI |
| Logs + read-only config | `GET /api/logs`, `GET /api/config` |
| **Follow tuning API + UI** | `POST /api/config/tuning`, TOOLS → FOLLOW TUNING |
| **Heartbeat auto-stop** | WS ping + `websocket.heartbeat_timeout_s` |
| **BLE RSSI tuning in config** | `ble_tracker.rssi_track`, etc. |
| Test plan M1 mobility | `docs/TEST_PLAN.md` |

## Next (recommended order)

### 1. Prove follow on the floor

- **T0–T4** tethered at desk (`docs/TEST_PLAN.md`)
- **M1** walk tests with pass-through or long cable
- Log in `docs/TEST_RESULTS.md`

### 2. Tag v1.0.0 when M1 passes

No need to wait for pure-battery **U0** unless you want that extra hardening.

### 3. Software upgrades (pick one)

| Feature | Effort | Notes |
|---------|--------|--------|
| YOLOv8 **pose** model | Medium | `yolov8m_pose_h10.hef`, better occlusion |
| BLE **PID** distance hold | Medium | v1 backlog — target RSSI slider |
| Second ultrasonic | Hardware | Forward-low sensor |
| `GET /api/status` battery % | Firmware | MegaPi `get_vin()` |

### 4. Pure battery (optional)

| Item | When |
|------|------|
| USB keep-alive module | Better Viking idle |
| `usb_max_current_enable=1` | Pi `config.txt` before long battery |
| **U0–U2** | Phone-only WiFi sessions |

## Explicitly defer (v1 scope)

- Android apps, voice, LLM, agent loop, extensions, world state, WSS auth
- Arbitrary shell from browser

## ROVER v1 backlog

See `~/Documents/projects/rover/ROVER_BACKLOG.md`. Map in `PROJECT.md` — only port items that fit the minimal ROVER2 charter.
