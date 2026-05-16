# ROVER2 WebSocket protocol (Phase 2)

Endpoint: `ws://<pi-host>:8082/ws`

## Client → server

| type | Fields | Notes |
|------|--------|-------|
| `drive` | `direction`, `speed` (0–1) | Hold-to-move; send `stop` on release |
| `stop` | — | Stop all drive motors |
| `grip` | `action`: `open` \| `close` | DC gripper open/close (PORT4B) |
| `arm` | `direction`: `up` \| `down` \| `stop`, `speed` (0–1) | Arm lift hold-to-move (PORT3B) |
| `arm_pulse` | `action`: `up` \| `down` | ~800 ms arm nudge (bench test) |
| `tracking` | `enabled`: boolean | Pi camera follow (Hailo); disables manual D-pad |
| `ping` | — | Server replies `pong` |

## Server → client

| type | Notes |
|------|-------|
| `telemetry` | Pushed every `websocket.telemetry_interval_s` (default 0.4 s) |
| `ack` | Command accepted |
| `pong` | Ping reply |
| `error` | `msg` field with reason |

### Telemetry fields

Same as `GET /api/status`: `serial_connected`, `firmware`, `motors_ready`, `ultrasonic_cm`, `ultrasonic_age_s`, `safety_enabled`, `safe_distance_cm`, `forward_blocked`, `uptime_s`, plus when tracking is configured: `tracking_available`, `tracking_enabled`, `tracking_hailo_ready`, `person_detected`, optional `tracking_error`.

## Disconnect safety

When the browser tab closes or the WebSocket drops, the server **stops drive motors** if that client was driving.

REST endpoints (`/api/drive`, etc.) remain for scripts and debugging.
