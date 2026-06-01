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
- WebSocket heartbeat: UI must ping while driving/following or motors stop after ~10s (heartbeat_timeout_s: 10.0)
- E-stop in UI stops motors and tracking

POWER
- Viking PN-964PD power bank — may sleep without USB load; pass-through or keep-alive dongle for battery

SERVICES (systemd)
- rover2-api (port 8082), rover-camera (port 8081), ollama (port 11434), hailo-ollama (port 8000)
- rover2-restore-wifi, rover2-virtual-usb-dongle (optional keepalive for power bank)
- hailo-ollama: ENABLED by default (backend=hailo). Chip sharing via group_id=rover2 + ROUND_ROBIN.

AGENT
- Primary: qwen2.5-instruct:1.5b on hailo-ollama (AI HAT+, port 8000). backend=hailo in config.yaml.
- CPU fallback: llama3.2:1b on ollama (port 11434) when hailo-ollama unavailable.
- Fast-path tools (logs, temps, alerts) use Pi HTTP only — no LLM call.
- gemma2:2b does NOT support tools on Ollama — do not use for agent.

TUNABLE VIA AGENT (config.yaml, live apply)
- body_tracker: confidence, centre_zone, target_bbox_width, turn_speed, forward_speed, avoid_default, frame_interval_s
- safety: safe_distance_cm, poll_interval_s
- ble_tracker: rssi_track, rssi_close, search_speed, fwd_speed, beacon_uuid
- drive: default_speed, max_speed
- arm: max_speed, invert
- websocket: telemetry_interval_s, heartbeat_timeout_s
- ultrasonic: poll_interval_s
"""
