"""Static ROVER2 hardware/software reference for the AI agent."""

ROVER2_KNOWLEDGE = """
ROVER2 — Raspberry Pi 5 companion robot (Makeblock MegaPi + AI HAT+ 2)

NETWORK
- eth0 (deploy): 192.168.70.11 — preferred for SSH and web UI :8082
- WiFi GUCZ-744: 192.168.250.254 — phone access when untethered
- Tailscale: 100.67.13.10
- Camera MJPEG: port 8081 (rover-camera.service)
- API + web UI: port 8082 (rover2-api.service)
- Only ONE of rover-api (v1 :8080) or rover2-api may use /dev/ttyUSB0

MEGA PI / FIRMWARE (rover2-basic-1.0.3)
- PORT1B / PORT2B: left/right drive (Ultimate 2.0 wiring)
- PORT3B: arm lift
- PORT4B: gripper open/close
- Ultrasonic: auto-scan PORT_1–8, usually PORT_8
- Serial: /dev/ttyUSB0 @ 115200

VISION & FOLLOW
- Hailo-10H YOLOv8m person detection on AI HAT+ 2
- Camera follow: turn toward person in frame, forward if bbox small
- BLE fallback: Samsung Flip 6 service UUID fcf1 (not MAC — Android randomises BLE MAC)
- Obstacle: forward ultrasonic < safe_distance_cm (default 40) blocks FWD; follow steers around

SAFETY
- WebSocket heartbeat: UI must ping while driving/following or motors stop after ~4s
- E-stop in UI stops motors and tracking

POWER
- Viking PN-964PD power bank — may sleep without USB load; pass-through or keep-alive dongle for battery
- hailo-ollama must stay DISABLED (conflicts with Hailo for follow)

SERVICES (systemd)
- rover2-api, rover-camera, ollama, rover2-restore-wifi, rover2-virtual-usb-dongle (optional)

AGENT
- Target: LLM on AI HAT+ via hailo-ollama (Pi is bridge only). backend=hailo in config.yaml.
- Fast-path tools (logs, temps, alerts) use Pi HTTP only — no LLM.
- gemma2:2b does NOT support tools on Ollama.

TUNABLE VIA AGENT (config.yaml, live apply)
- body_tracker: confidence, centre_zone, target_bbox_width, turn_speed, forward_speed, avoid_default, frame_interval_s
- safety: safe_distance_cm, poll_interval_s
- ble_tracker: rssi_track, rssi_close, search_speed, fwd_speed, beacon_uuid
- drive: default_speed, max_speed
- arm: max_speed, invert
- websocket: telemetry_interval_s, heartbeat_timeout_s
- ultrasonic: poll_interval_s
"""
