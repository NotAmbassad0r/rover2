# HANDOFF.md — ROVER2

Last updated: 2026-05-16 (AI agent + VLM + metrics charts + threshold alerts + global voice + TTS)

Greenfield minimal stack: MegaPi motors, **arm lift**, gripper, ultrasonic, web control, **Hailo person follow**, **BLE beacon fallback follow**, **on-device AI (LLM + VLM)**. Runs **alongside** ROVER v1 on a separate port; **do not** bind both APIs to `/dev/ttyUSB0` at once.

## Project priorities (non-negotiable order)

**ALWAYS make the robot as energy efficient as possible. Every change, no exceptions.**

1. **Energy / resource efficiency** — CPU, memory, temperature, power. If something is idle, it uses zero CPU. Close streams, sleep, use events — never spin.
2. **Performance** — speed and responsiveness of what is already built.
3. **Features** — only after 1 and 2 are satisfied.

Hard rules applied to every change:
- Idle = zero CPU. No polling, no open streams, no spin loops when not active.
- Hailo does all inference. Never run vision/ML on CPU.
- CPU governor: `schedutil` always. Not `performance`, not `ondemand`.
- `stress-ng` / virtual USB dongle: only on battery. Always off on mains.
- Verify idle CPU before and after every change.
- rover2-api memory target: <150 MB RSS.

**Claude Code:** Start every session with: `Read HANDOFF.md in full before making changes.`

---

## Who / where

| Item | Value |
|------|--------|
| Developer | ambassad0r |
| Dev machine | central-computer — 192.168.20.11 (Kubuntu 26.04) |
| Dev repo | `~/Documents/projects/rover2/` |
| Pi deploy path | `/opt/rover2/` |
| Pi hostname | `rover` (Pi 5 8GB + AI HAT+ 2; same host as v1) |

### Pi network

| Interface | IP |
|-----------|-----|
| eth0 | **192.168.70.11** (preferred — use for deploy, UI, SSH) |
| WiFi | 192.168.250.254 |
| Tailscale | 100.67.13.10 |

**Dev machine has no route to 192.168.250.254** — deploy always via eth0 (rover-eth). Use WiFi IP only from a device on the same WiFi (phone, laptop on local network).

### SSH

```bash
ssh ambassad0r@192.168.70.11       # eth0 (preferred)
ssh rover-eth                      # ~/.ssh/config → 192.168.70.11
ssh ambassad0r@192.168.250.254     # WiFi (from same-subnet device only)
```

### WiFi down (`wlan0` NO-CARRIER)

WiFi credentials live in **`/boot/firmware/network-config`** (SSID **`GUCZ-744`** → **192.168.250.254**). **`rover2-restore-wifi.service`** runs on every boot and re-applies netplan if `wlan0` is not connected.

Manual fix (eth0): `sudo bash /opt/rover2/scripts/restore_wifi_from_boot.sh`  
Web UI: **TOOLS → RESTORE WIFI** (`POST /api/maintenance/wifi-restore`) — needs `systemd/rover2-wifi.sudoers` (installed by `deploy_pi.sh`).  
New SSID: `sudo WIFI_SSID=... WIFI_PASSWORD=... bash /opt/rover2/scripts/setup_wifi.sh`.

---

## Phase status

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 0 | Motors, gripper, ultrasonic, web D-pad | **Done** |
| 1 | Forward obstacle stop (ultrasonic < 40 cm) | **Verified** |
| 2 | WebSocket D-pad, live telemetry, stop on disconnect | **Done** |
| 2b | Arm lift (PORT3B), web + API | **Done** (firmware 1.0.3) |
| 3 | Camera / Hailo person follow (FOLLOW toggle) | **Done — verified on robot** |
| 4 | BLE beacon fallback follow (Flip 6 via fcf1 UUID) | **Done — verified** |
| 5 | Web TOOLS + DIAG + CHAT + METRICS + AI Agent + VLM + Alerts | **Done** |
| test1 | Follow mobility (M1: pass-through / long cable OK) | **Pending** — blocked on powerbank (auto-shutoff) |
| v1.0.0 | M1 pass → merge dev→main | Pending M1 / powerbank resolved |

---

## Architecture

```
Browser (index.html)  http://192.168.70.11:8082/
    → WebSocket ws://<pi>:8082/ws        drive, grip, arm, tracking, telemetry, alerts
    → HTTP REST /api/*                   drive, diagnostics, scripts, WiFi, AI agent, metrics
    → Camera preview                     http://<pi>:8081/stream  (rover-camera.service)

    ── Python services (pi/) ──────────────────────────────────────────────────
    pi/main.py          Entrypoint: wires all components, starts uvicorn
    pi/server.py        FastAPI: 42+ REST endpoints + WebSocket hub
    pi/ws_control.py    WebSocket hub; telemetry push with extra fields (alerts)
    pi/safety.py        Ultrasonic forward gate
    pi/body_tracker.py  Hailo YOLOv8m person follow + BLE fallback + obstacle steer
    pi/body_tracker_parse.py  YOLO NMS output parser (detection-major)
    pi/follow_nav.py    Obstacle steer + BLE RSSI homing helpers
    pi/ble_tracker.py   BLE RSSI beacon scanner (bleak)
    pi/megapi.py        pyserial, ultrasonic poll
    pi/diagnostics.py   gather_diagnostics(), SCRIPT_CATALOG, WiFi helper
    pi/metrics_store.py SQLite time-series metrics DB (~5 s sample interval)
    pi/log_buffer.py    In-process log ring for /api/logs
    pi/chat_router.py   Regex NL command router (zero-LLM fast path, 14 commands)
    pi/agent.py         RoverAgent: Ollama tool-use agentic loop (20 tools, fast-path)
    pi/vlm_engine.py    VLMEngine: Hailo Qwen2-VL-2B-Instruct scene description

    ── Hailo AI HAT+ 2 ───────────────────────────────────────────────────────
    YOLOv8m_h10.hef     Body tracker (FOLLOW / DETECT)  group_id=rover2
    Qwen2-VL-2B.hef     VLM scene description           group_id=rover2
    ← ROUND_ROBIN scheduler: both models share the chip without mode-switching

    ── Ollama (system service on Pi) ─────────────────────────────────────────
    llama3.2:1b         Agent LLM (tool-use capable; gemma2:2b does NOT support tools)
    gemma2:2b           Available but NOT used for tool-use
    deepseek-r1:1.5b    Available

    ── Firmware ──────────────────────────────────────────────────────────────
    firmware/rover2_basic/  rover2-basic-1.0.3
    PORT1B/2B drive, PORT3B arm, PORT4B gripper, ultrasonic auto-scan
```

| Service | Port | Notes |
|---------|------|--------|
| `rover2-api.service` | **8082** | ROVER2 control + follow + AI |
| `rover-camera.service` | **8081** | MJPEG (v1 stack; shared) |
| `ollama.service` | **11434** | Local LLM — llama3.2:1b for agent tool-use |
| `rover-api` (v1) | 8080 | Stop when testing ROVER2 serial |
| `hailo-ollama` | — | **Permanently disabled** — conflicts with Hailo chip ownership |
| `rover2-restore-wifi.service` | — | Boot: re-apply WiFi from `/boot/firmware/network-config` |
| `rover2-virtual-usb-dongle.service` | — | Optional ~12% CPU keep-alive for Viking bank (see `docs/POWERBANK.md`) |

---

## What works (confirmed 2026-05-16)

- D-pad tuned for Ultimate 2.0 (`drive.direction_map`: forward `(1,-1)`, etc.)
- Gripper open/close (PORT4B)
- **Arm lift** hold up/down + nudge pulse (PORT3B; firmware 1.0.3)
- Ultrasonic on **PORT_8** (firmware auto-scan; logs `ultrasonic on PORT_8`)
- Live ultrasonic via WebSocket telemetry
- Safety: forward blocked when distance < 40 cm; FOLLOW steers around (turn toward person/BLE)
- WebSocket: drive, stop, grip, arm, tracking; stop on disconnect; threshold alerts pushed in telemetry
- **Live camera** in UI (loads `:8081/stream` on same hostname)
- **Camera FOLLOW** — Hailo YOLOv8m tracks person, LEFT/RIGHT/FWD/HOLD decisions
- **BLE fallback follow** — when camera loses person >2 s, rover rotates to re-acquire via RSSI gradient
- BLE BEACON row in web UI shows SCANNING / SEEN (dBm) / FOLLOWING (dBm)
- `hailo-ollama.service` permanently disabled via deploy_pi.sh + `Conflicts=` in systemd unit
- Firmware flash: `./scripts/flash_firmware.sh` (default Pi **192.168.70.11**)
- `deploy_pi.sh` → rsync `pi/`, `scripts/`, disable hailo-ollama, link Hailo, systemd refresh
- **Web TOOLS tab** — diagnostics, script catalog, follow tuning sliders, RESTORE WIFI
- **Web DIAG tab** — full system diagnostics with charts
- **Web CHAT tab** — NL commands, VLM scene description, AI agent, 16 preset buttons, voice input
- **Web METRICS tab** — live Chart.js graphs (CPU%, Temp °C, RAM%) with 15m/30m/1h range + "ASK AI" per chart
- **Global alert bar** — threshold violations shown at top of every tab; "ASK AI WHAT'S WRONG" button
- **Global voice bar** — mic button always visible; smart routing (control cmd → chat, question → agent)
- **TTS toggle** — agent replies spoken aloud (first 2 sentences); new alerts announced
- **AI Agent** — Ollama llama3.2:1b + 20 tools; 9 fast-path patterns answer in <1 s without LLM
- **VLM scene description** — `/api/vision/describe` → Hailo Qwen2-VL snapshot caption
- **NL commands** — "follow me / stop / describe / grip open" via chat_router (regex, zero-LLM)
- **Threshold alerts** — server monitors CPU (sustained), temp, RAM, disk; pushes via WebSocket
- `GET /api/metrics/history?metric=X&minutes=N` — time-series from SQLite
- `GET /api/alerts/current` — current threshold violations
- `GET /api/robot/capabilities` — self-describing endpoint list (42 endpoints)
- Local tests: `./scripts/run_local_tests.sh` (20 tests incl. diagnostics + config mask)

---

## AI / LLM system

### On-device AI stack (fully offline — no cloud, no data egress)

```
Browser voice/text
    → /api/chat          chat_router.py  → direct NL commands (0 ms LLM)
    → /api/agent/chat    agent.py        → fast-path (<1 s) or Ollama (10–40 s)
    → /api/vision/describe  vlm_engine.py → Hailo VLM snapshot caption
```

### chat_router.py — zero-LLM NL commands

14 patterns, regex-matched. Returns action + reply without touching Ollama.

| Pattern | Action |
|---------|--------|
| stop, halt | stop motors |
| forward / go | drive forward 2 s |
| back / backward | drive back 2 s |
| left / right | turn 2 s |
| follow me / start follow | enable FOLLOW |
| unfollow / stop follow | disable FOLLOW |
| detect / start detect | detect-only mode |
| undetect / stop detect | off |
| describe / what do you see | VLM snapshot caption |
| grip / open / close | gripper |
| arm up / arm down | arm lift pulse |

### agent.py — agentic tool-use loop

- **Model:** `llama3.2:1b` via Ollama on Pi (fully local, no internet)
- **Tools:** 20 total — 16 read-only + 4 dangerous (require Confirm button)
- **Fast-path:** 9 patterns skip Ollama entirely, call tool directly, format result in Python

| Fast-path query | Tool called | Response time |
|-----------------|-------------|---------------|
| temperature / how hot | get_diagnostics | ~0.9 s |
| all services / list services | list_services | ~0.2 s |
| disk usage / storage | get_disk_details | ~0.3 s |
| wifi / wireless / signal | get_wifi_info | ~0.3 s |
| memory / RAM | get_diagnostics | ~0.9 s |
| hailo / ai chip | check_hailo | ~0.3 s |
| capabilities / what can you do | get_robot_capabilities | ~0.2 s |
| health check / full health | get_diagnostics + list_services (parallel) | ~1.0 s |
| any alerts / any issues | get_alerts | ~0.2 s |

**Read-only tools (model calls autonomously):**
`get_status`, `get_top_processes`, `get_diagnostics`, `get_logs`, `get_journal`,
`list_services`, `get_metric`, `get_wifi_info`, `get_disk_details`, `ping_host`,
`check_hailo`, `get_camera_health`, `get_db_stats`, `get_robot_capabilities`,
`get_alerts`, `update_config`, `set_robot_mode`

**Dangerous tools (require user CONFIRM button):**
`restart_service`, `stop_service`, `start_service`, `reboot_pi`

**Adding new tools:**
1. Add definition to `_READ_TOOLS` or `_DANGEROUS_TOOLS` in `agent.py`
2. Add case to `_run_tool()`
3. Optionally add fast-path entry to `_FAST_PATTERNS`
No other changes needed.

**Limits:** `_MAX_TOOL_ROUNDS = 4`, `_OLLAMA_TIMEOUT = 60 s`

### vlm_engine.py — scene description

- **Model:** `Qwen2-VL-2B-Instruct.hef` on Hailo-10H
- **Lazy-loaded:** model not loaded at startup; loads on first `describe()` call
- **Shares Hailo chip** with body_tracker via `group_id="rover2"` + ROUND_ROBIN
- `max_tokens: 128` (keep short; longer = more Hailo time)

### Dangerous action flow

```
Model proposes stop_service / restart_service / reboot_pi
    → AgentTurn(reply, tool_log, action_proposal)
    → UI shows red confirm bar: "Agent proposes: <name> — <reason>"
    → User clicks CONFIRM → POST /api/agent/confirm → executed
    → User clicks CANCEL → dropped
```

### Threshold alert system

Evaluated every metrics tick (~5 s). Pushed via WebSocket `alerts` field.

| Metric | Threshold | Severity |
|--------|-----------|----------|
| temp_cpu_thermal | ≥ 78°C | warn |
| temp_cpu_thermal | ≥ 85°C | crit |
| temp_rp1_adc | ≥ 75°C | warn |
| ram_percent | ≥ 85% | warn |
| disk_percent | ≥ 80% | warn |
| cpu_percent (sustained) | ≥ 85% for 30 s | warn |

---

## Follow behaviour

**Design intent (test1):**
1. **Camera** — see the operator and follow in frame (primary).
2. **BLE** — when not in frame, home on Flip 6 `fcf1` RSSI as tightly as hardware allows (secondary).
3. **Obstacles** — forward ultrasonic < 40 cm: do not ram; **steer** while keeping camera/BLE follow active.

**Camera follow** (primary):
- Hailo YOLOv8m NMS output — detection-major format `(80, 100, 5)` — see `body_tracker_parse.py`
- Person centred: HOLD; left of centre: LEFT turn; right: RIGHT turn; too far: FWD
- `turn_speed: 100`, `forward_speed: 120`, `confidence: 0.40`, `centre_zone: 0.30`

**BLE fallback** (secondary — kicks in when camera loses person):
- 2 s grace period after camera LOST before BLE activates
- Rotates slowly (speed 50), watches RSSI trend — flips direction if signal worsens
- Advances (speed 60) if RSSI > -75 dBm; holds if RSSI > -60 dBm (very close)
- While advancing, if RSSI falls over recent samples → rotate to re-home
- Camera re-acquiring immediately cancels BLE and resumes camera follow
- BLE timer also starts immediately when FOLLOW is enabled while person is not in frame
- **Limit:** RSSI only; "exact" means hill-climb rotate + advance, not compass bearing

**BLE device matching** (Flip 6):
- Matches by service UUID `0000fcf1-0000-1000-8000-00805f9b34fb` (Samsung service, always advertised)
- NOT by MAC (Android randomises BLE MAC) or name (not included in BLE advertisement)
- Config: `ble_tracker.beacon_uuid` in `pi/config.yaml`
- Fallback chain: `beacon_uuid` → `device_mac` → `device_name` substring

**Ultrasonic safety + follow avoidance** (always active):
- `SafetyMonitor` hard-blocks **forward** drive commands when distance < 40 cm
- During FOLLOW, `body_tracker` + `follow_nav.steer_around_obstacle()` replace blocked FWD with a turn
- **Limit:** one forward-facing ultrasonic — good for doorways/corners ahead, not full room mapping

---

## Hardware ports (MegaPi)

| Function | Port |
|----------|------|
| Left drive | PORT1B |
| Right drive | PORT2B |
| **Arm lift** | **PORT3B** |
| Gripper open/close | PORT4B |
| Ultrasonic | Auto-scan PORT_1–8 (locked on first valid echo; usually PORT_8) |

If arm moves the wrong way: `arm.invert: true` in `pi/config.yaml` (Pi only; no reflash).

---

## Firmware

| Item | Value |
|------|--------|
| Sketch | `firmware/rover2_basic/rover2_basic.ino` |
| Version | **`rover2-basic-1.0.3`** (`motors: 3` in ready event) |
| Arm commands | `{"cmd":"arm","speed":N}` (−255…255, 0=stop arm); `{"cmd":"arm","action":"up"\|"down"}` = ~800 ms pulse |
| Ultrasonic | `distanceCm(400)` — readings ≥400 = no echo |

### Flash from central-computer

```bash
cd ~/Documents/projects/rover2
./scripts/flash_firmware.sh              # default 192.168.70.11
```

Stops `rover2-api`, flashes via `/opt/rover2/tools/avrdude`, restarts service.

---

## Deploy

```bash
cd ~/Documents/projects/rover2
./deploy_pi.sh
```

- Auto-picks first reachable host: `rover-eth`, `192.168.70.11`, `rover`, WiFi, Tailscale.
- Default fallback IP: **192.168.70.11**.
- Disables `hailo-ollama.service` and `hailo-tappas-playground.service` permanently.
- Runs `scripts/link_hailo_for_rover2.sh` on Pi (symlinks system `hailo_platform` into ROVER2 venv).
- Sideloads `httpx` and `bleak` (+ `dbus_fast`, `async_timeout`) from dev machine if pip fails.
- Updates `systemd/rover2-api.service` if present in repo.
- Installs `rover2-restore-wifi.service`, `systemd/rover2-wifi.sudoers`.

```bash
ROVER2_SKIP_VIRTUAL_DONGLE=1 ./deploy_pi.sh   # code-only deploy, skip power profile
```

Restart only:
```bash
ssh rover-eth "sudo systemctl restart rover2-api.service"
```

Logs:
```bash
ssh rover-eth "journalctl -u rover2-api.service -f"
```

Verify:
```bash
ssh rover-eth "curl -s http://127.0.0.1:8082/api/status | python3 -m json.tool"
# Expect: tracking_available true, tracking_hailo_ready true, ble_available true, ble_seen true

ssh rover-eth "curl -s http://127.0.0.1:8082/api/agent/status"
# Expect: {"base":"http://127.0.0.1:11434","model":"llama3.2:1b","available":true}

ssh rover-eth "curl -s http://127.0.0.1:8082/api/alerts/current"
# Expect: {"alerts":[],"count":0}
```

---

## Web UI

**http://192.168.70.11:8082/** (WiFi: http://192.168.250.254:8082/)

**Hard-refresh after deploy:** `Ctrl+Shift+R` — the HTML is not aggressively cached but browsers sometimes hold old copies. Check the page source first line for version comment `<!-- v2025-05-16c -->`.

**Five tabs:** CONTROL | TOOLS | DIAG | CHAT | METRICS

### CONTROL tab

| Section | Notes |
|---------|--------|
| STATUS | WEBSOCKET, SERIAL, ULTRASONIC, SAFETY, FOLLOW, PERSON, BLE BEACON |
| LIVE CAMERA | MJPEG from port **8081** |
| ARM LIFT | Hold ▲/▼; NUDGE = short pulse |
| DRIVE | D-pad; disabled while FOLLOW on |
| DETECT | Hailo runs, logs detections, no motor output |
| FOLLOW | Camera follow + BLE fallback; needs `tracking_available` |

### TOOLS tab

| Section | Notes |
|---------|--------|
| DIAGNOSTICS | Cached report on open; **RUN DIAGNOSTICS** = fresh `POST /api/diagnostics/run` |
| RESTORE WIFI | Re-apply WiFi from boot config |
| SCRIPTS | Maintenance script catalog — dev commands |
| LOGS | `/api/logs` — refresh / clear |
| CONFIG | Read-only YAML snapshot |
| FOLLOW TUNING | Sliders → `POST /api/config/tuning` (live + saves yaml) |

### DIAG tab

Full system diagnostics: hardware, network, software, system status with charts.

### CHAT tab

| Section | Notes |
|---------|--------|
| AI: ON-DEVICE banner | "AI: ON-DEVICE · NO CLOUD · Hailo-10H neural chip · Ollama LLM · zero data egress" |
| VLM ENGINE status | Shows loaded/idle/skip |
| NL COMMANDS | Text or voice → chat_router → direct action (zero LLM cost) |
| QUICK COMMANDS | stop / follow / unfollow / detect / describe / grip open\|close / arm up\|down |
| AI AGENT | Full agentic diagnostic panel with tool call log |
| CONFIRM bar | Red bar appears when agent proposes dangerous action; CONFIRM / CANCEL |
| 16 agent presets | HIGH CPU, TEMPS, DETECT ISSUE, CHECK LOGS, HEALTH CHECK, ALL SERVICES, DISK USAGE, WIFI STATUS, HAILO STATUS, READY TO FOLLOW?, STOP CAMERA, MEMORY, CAPABILITIES, AI STATUS, BLE ISSUE, CRASH LOGS |

### METRICS tab

| Section | Notes |
|---------|--------|
| Time range | 15m / 30m / 1h selector; ⟳ manual refresh |
| CPU % chart | Live Chart.js line graph; current/avg/peak stats |
| TEMPERATURE °C | Both cpu_thermal and rp1_adc sensors |
| RAM % | With warning threshold indicator |
| ASK AI button | Per chart — injects current/avg/peak into agent question |
| Voice hint | Shows example voice commands for this tab |

### Global elements (all tabs)

| Element | Notes |
|---------|--------|
| Alert bar | Red banner at top when thresholds exceeded; "ASK AI WHAT'S WRONG" |
| 🎤 SPEAK TO ROVER | Global voice button; smart routing: control words → chat, questions → agent |
| 🔇/🔊 TTS toggle | When on: agent replies + alerts spoken aloud (first 2 sentences) |

---

## WebSocket protocol

Endpoint: `ws://192.168.70.11:8082/ws`

**Client → server:** `drive`, `stop`, `grip`, `arm`, `arm_pulse`, `tracking`, `ping`  
**Server → client:** `telemetry`, `ack`, `pong`, `error`

Telemetry includes: `tracking_available`, `tracking_enabled`, `tracking_hailo_ready`, `person_detected`, `ble_active`, `ble_available`, `ble_seen`, `ble_rssi`, **`alerts`** (list of threshold violations).

---

## REST API

### Core

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/status` | Full snapshot |
| POST | `/api/drive` | Drive (HTTP fallback) |
| POST | `/api/stop` | Stop all motors + arm |
| POST | `/api/grip` | `{"action":"open"\|"close"}` |
| POST | `/api/arm` | `{"direction":"up","speed":1}` or `{"action":"up"}` pulse |
| POST | `/api/tracking` | `{"enabled":true\|false}` or `{"detect_only":true}` |
| GET | `/api/ultrasonic` | Force one read |

### AI / Chat

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/chat` | NL command → chat_router → action + reply |
| GET | `/api/vision/describe` | Snapshot → Hailo VLM → scene caption |
| GET | `/api/chat/status` | VLM + agent readiness |
| POST | `/api/agent/chat` | `{"messages":[...]}` → agentic turn → reply + tool_log |
| POST | `/api/agent/confirm` | Execute pending dangerous action |
| GET | `/api/agent/status` | Ollama reachability + model name |

### Diagnostics

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/diagnostics` | Cached hardware/network/software/system report |
| POST | `/api/diagnostics/run` | Fresh diagnostics (20 s timeout) |
| GET | `/api/diagnostics/full` | Extended diagnostics (temperatures, per-core CPU, net rates) |
| GET | `/api/diagnostics/processes` | Top 12 processes by CPU with full cmdline |
| GET | `/api/diagnostics/journal?service=X&lines=N` | systemd journal (allowed services only) |
| GET | `/api/diagnostics/services` | All rover service statuses (active/enabled) |
| GET | `/api/diagnostics/wifi` | SSID, signal, IP, gateway, link details |
| GET | `/api/diagnostics/disk` | Per-partition + du for key directories |
| GET | `/api/diagnostics/wifi/ping?host=X` | Ping test (3 packets) |
| GET | `/api/diagnostics/hailo` | Hailo chip + HEF model status |
| GET | `/api/diagnostics/db` | Metrics SQLite stats |
| GET | `/api/robot/capabilities` | Self-describing feature + endpoint list |

### Metrics

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/metrics/history?metric=X&minutes=N` | Time-series from SQLite |
| GET | `/api/alerts/current` | Current threshold violations |
| GET | `/metrics` | Prometheus exposition format (for Grafana) |

### Maintenance

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/scripts` | Maintenance script catalog |
| POST | `/api/maintenance/wifi-restore` | Re-apply WiFi from boot config (sudo) |
| POST | `/api/maintenance/restart-service` | Restart rover2-api (sudo) |
| POST | `/api/maintenance/restart-pi` | Reboot Pi (sudo) |
| POST | `/api/maintenance/service` | `{"action":"start"\|"stop"\|"restart","service":"X"}` (whitelist) |
| GET | `/api/logs` | Last ~200 in-process log records |
| DELETE | `/api/logs` | Clear log buffer |
| GET | `/api/config` | Read-only config.yaml (secrets masked) |
| POST | `/api/config/tuning` | Follow / safety / BLE tuning (persist + runtime) |
| GET | `/stream` | MJPEG proxy (503 if httpx missing — use :8081) |
| GET | `/snapshot` | JPEG snapshot proxy |
| POST | `/api/firmware/upload` | Stage hex |
| POST | `/api/firmware/flash` | avrdude flash |

**Service management whitelist** (`_ALLOWED_SERVICES` in server.py):  
`rover2-api`, `rover-camera`, `ollama`, `rover2-powerbank-keepalive`, `rover2-virtual-usb-dongle`, `rover2-restore-wifi`, `cpu-governor`, `hailo-ollama`, `bluetooth`, `NetworkManager`

**Sudoers** (`systemd/rover2-wifi.sudoers`, installed to `/etc/sudoers.d/rover2-wifi`):  
Allows `ambassad0r` to run `systemctl stop/start/restart` for the above services without password.

---

## Config highlights (`pi/config.yaml`)

```yaml
server:
  port: 8082

drive:
  default_speed: 120
  # direction_map:   # forward: [1, -1]  etc.

arm:
  port: PORT3B
  max_speed: 150
  invert: false      # true if up/down reversed

safety:
  enabled: true
  safe_distance_cm: 40

body_tracker:
  enabled: true
  camera_url: http://127.0.0.1:8081/stream
  hef_path: /opt/rover/models/yolov8m_h10.hef
  hailo_group_id: rover2
  turn_speed: 100
  forward_speed: 120
  confidence: 0.40
  centre_zone: 0.30
  target_bbox_width: 0.35
  avoid_default: left   # FWD blocked, person centred in frame

ble_tracker:
  enabled: true
  device_name: "-Lars's Z flip 6"
  device_mac: "F0:05:1B:0A:E0:4C"
  beacon_uuid: "0000fcf1-0000-1000-8000-00805f9b34fb"
  rssi_track: -75
  rssi_close: -60
  search_speed: 50
  fwd_speed: 60

agent:
  enabled: true
  ollama_base: http://127.0.0.1:11434   # Ollama runs locally on the Pi
  model: llama3.2:1b                    # tool-use capable (gemma2:2b does NOT support tools)
  rover_base: http://127.0.0.1:8082

vlm:
  enabled: true
  hef_path: /opt/rover/models/Qwen2-VL-2B-Instruct.hef
  hailo_group_id: rover2
  camera_url: http://127.0.0.1:8081/snapshot
  max_tokens: 128                       # keep short; longer = more Hailo time
```

---

## Hailo / camera follow

| Item | Location |
|------|----------|
| YOLO Model | `/opt/rover/models/yolov8m_h10.hef` (shared with v1) |
| VLM Model | `/opt/rover/models/Qwen2-VL-2B-Instruct.hef` |
| Python | System `hailo_platform` → linked into ROVER2 venv by `scripts/link_hailo_for_rover2.sh` |
| YOLO code | `pi/body_tracker.py`, `pi/body_tracker_parse.py` |
| VLM code | `pi/vlm_engine.py` |
| NMS format | Detection-major `(80, 100, 5)` — field order: `score, y0, x0, y1, x1` |
| Chip sharing | `group_id="rover2"` + `ROUND_ROBIN` — both models share chip in same process |

**Do not set `PYTHONPATH=/usr/lib/python3/dist-packages` in systemd** — breaks FastAPI/pydantic. Use the link script only.

If Hailo init fails (`HAILO_OUT_OF_PHYSICAL_DEVICES`):
```bash
ssh rover-eth "sudo systemctl stop hailo-ollama hailo-tappas-playground"
ssh rover-eth "sudo systemctl restart rover2-api"
# Permanent fix already in deploy_pi.sh — re-deploy to apply
```

---

## BLE tracker

| Item | Detail |
|------|--------|
| Library | `bleak` 3.0.2 (sideloaded — TCP/443 blocked) |
| Deps | `dbus_fast` 4.2.5 (cp313 aarch64), `async_timeout` 5.0.1 |
| Target | Flip 6 Samsung service UUID `fcf1` |
| Why not MAC | Android 10+ randomises BLE MAC every ~15 min |
| Why not name | Android does not include device name in BLE advertisements |
| Scan mode | Active (passive mode not supported by this BlueZ version) |
| RSSI EMA | α=0.3 (new) + 0.7 (old); lost after 3 s of silence |

---

## Relation to ROVER v1

| | ROVER v1 | ROVER2 |
|---|----------|--------|
| Repo | `~/Documents/projects/rover/` | `~/Documents/projects/rover2/` |
| Pi path | `/opt/rover/` | `/opt/rover2/` |
| API | :8080 | :8082 |
| Serial | `/dev/ttyUSB0` | same — **only one stack at a time** |

```bash
ssh rover-eth "sudo systemctl stop rover-api.service"      # free serial for ROVER2
ssh rover-eth "sudo systemctl stop rover2-api.service"     # free serial for v1
```

---

## Key files

| Path | Role |
|------|------|
| `pi/main.py` | Entrypoint: SafetyMonitor, BLETracker, BodyTracker, VLMEngine, RoverAgent, uvicorn |
| `pi/server.py` | FastAPI: 42+ REST endpoints, WebSocket hub, metrics loop, alert state |
| `pi/ws_control.py` | WebSocket hub + telemetry push; `set_telemetry_extra()` for alert injection |
| `pi/safety.py` | Ultrasonic forward gate |
| `pi/body_tracker.py` | Hailo person follow + BLE fallback + obstacle steer |
| `pi/body_tracker_parse.py` | YOLO NMS parsing (detection-major; unit tests) |
| `pi/follow_nav.py` | Pure helpers: obstacle steer, BLE RSSI homing (unit tests) |
| `pi/ble_tracker.py` | BLE RSSI scanner (bleak, UUID matching, EMA smoothing) |
| `pi/megapi.py` | Serial bridge |
| `pi/diagnostics.py` | `gather_diagnostics()`, `SCRIPT_CATALOG`, WiFi restore helper |
| `pi/metrics_store.py` | SQLite time-series metrics (~5 s interval, 7-day retention) |
| `pi/log_buffer.py` | In-process log ring for `/api/logs` |
| `pi/chat_router.py` | Regex NL command router — 14 commands, zero-LLM |
| `pi/agent.py` | RoverAgent: Ollama tool-use loop, 20 tools, fast-path patterns |
| `pi/vlm_engine.py` | VLMEngine: lazy-loaded Hailo VLM for scene description |
| `pi/config_public.py` | Secret masking for `/api/config` |
| `pi/arm_control.py` | Arm PWM mapping |
| `pi/camera_proxy.py` | `/stream` proxy (needs httpx) |
| `pi/web/static/index.html` | Web UI (5 tabs: CONTROL, TOOLS, DIAG, CHAT, METRICS) |
| `pi/tests/test_diagnostics.py` | Diagnostics catalog + report shape |
| `pi/tests/test_body_tracker.py` | Body tracker unit tests |
| `pi/tests/test_follow_nav.py` | follow_nav helper unit tests |
| `firmware/rover2_basic/` | Active firmware (rover2-basic-1.0.3) |
| `scripts/flash_firmware.sh` | Compile + flash |
| `scripts/run_local_tests.sh` | Dev unit tests (no Pi) |
| `scripts/check_rover_ready.sh` | Pi preflight (ping, API, camera, Hailo) |
| `scripts/restore_wifi_from_boot.sh` | One-shot WiFi restore |
| `scripts/restore_wifi_if_needed.sh` | Boot + API WiFi restore |
| `scripts/setup_wifi.sh` | New SSID/password |
| `scripts/setup_normal_day_power.sh` | Virtual dongle + suspend mask (battery days) |
| `scripts/link_hailo_for_rover2.sh` | Hailo symlink into ROVER2 venv |
| `scripts/install_avrdude_on_pi.sh` | One-time avrdude in `/opt/rover2/tools/` |
| `deploy_pi.sh` | Deploy + disable hailo-ollama + Hailo link + sudoers + restart |
| `systemd/rover2-api.service` | systemd unit (`Conflicts=hailo-ollama.service`) |
| `systemd/rover2-restore-wifi.service` | Boot WiFi restore |
| `systemd/rover2-wifi.sudoers` | Passwordless WiFi restore + service management for `ambassad0r` |
| `docs/PROTOCOL.md` | Serial JSON |
| `docs/WEBSOCKET.md` | WebSocket types |
| `docs/TEST_PLAN.md` | Tethered T0–T4 (now); untethered U0–U2 (after keep-alive) |
| `docs/TEST_RESULTS.md` | Session test log |
| `docs/ROADMAP_UNTIL_HARDWARE.md` | Safe software work before USB keep-alive module |
| `docs/POWERBANK.md` | Viking bank + virtual dongle |

Legacy (do not flash): `firmware/rover2_firmware/`

---

## Power bank (test1) — Viking PN-964PD

Pi 5 on **27 000 mAh Viking PN-964PD**. Viking auto-shuts off without USB load.

**You can run test1 / M1 mobility without the keep-alive dongle:**

| Mode | How |
|------|-----|
| Lab | Mains + eth **192.168.70.11** |
| Walk test | Viking **pass-through** (charger → IN, Pi → OUT) or long USB-C |
| Short battery | Virtual dongle service + double-tap bank ON |

**Optional:** USB keep-alive in spare port → easier pure-battery **U0** (phone on WiFi only).

See `docs/POWERBANK.md`, `docs/TEST_PLAN.md` **M1** vs **U0**.

---

## Known issues / notes

1. **Powerbank auto-shutoff** — Viking PN-964PD shuts off without USB load. test1 / M1 mobility blocked until resolved: use pass-through cable OR install USB keep-alive dongle.
2. **llama3.2:1b for agent** — gemma2:2b does NOT support tool-use in Ollama's API despite the docs. llama3.2:1b is the correct model. Do not switch to gemma2:2b for agent.
3. **VLM lazy load** — Qwen2-VL is not loaded at startup. First `/api/vision/describe` call triggers load (may take 10–15 s). Subsequent calls use ROUND_ROBIN sharing with YOLO.
4. **hailo-ollama permanently disabled** — conflicts with Hailo chip ownership. Do not re-enable while ROVER2 is in use.
5. **Ultrasonic** — Auto-scan locks port; power-cycle MegaPi to rescan if sensor moved.
6. **MakeBlock timeout** — `distanceCm(N)` returns N on timeout; firmware rejects ≥400 cm.
7. **httpx** — Sideloaded by `deploy_pi.sh` if pip fails. Camera UI uses **:8081**; `/stream` on 8082 returns 503 without httpx.
8. **Pi TCP/443 blocked at gateway** — ICMP/DNS work; TCP/443 silently dropped. `pip install` fails for external packages. `deploy_pi.sh` sideloads wheels via `scp`. Pip timeout = 5 s in `~/.config/pip/pip.conf`.
9. **BLE MAC randomisation** — Android 10+ randomises BLE MAC every ~15 min. Match by UUID (`fcf1`) instead.
10. **BLE in dense apartment** — Many neighbour Samsung devices advertise `fcf1`. RSSI gradient follow is rough but sufficient for short-range camera re-acquisition.
11. **Browser cache** — After deploy, always hard-refresh (`Ctrl+Shift+R`). Check page source first line for version comment.
12. **Dev machine WiFi routing** — Dev machine (192.168.20.11) has no route to Pi WiFi (192.168.250.254). Always deploy via eth0.
13. **FOLLOW vs manual drive** — D-pad disabled while FOLLOW on; manual drive disables FOLLOW.

---

## Testing

**Plan:** `docs/TEST_PLAN.md`  
**Log results:** `docs/TEST_RESULTS.md`

| Phase | When | Tests |
|-------|------|--------|
| **T0–T4** | Now (eth + mains) | Preflight, D-pad, DETECT/FOLLOW, obstacle steer, BLE fallback |
| **U0–U2** | After powerbank resolved | Untethered WiFi + battery, 10+ min follow |

```bash
./scripts/run_local_tests.sh              # dev PC — 20 unit tests
./scripts/check_rover_ready.sh            # Pi preflight (default 192.168.70.11)
```

Web: **TOOLS → RUN DIAGNOSTICS** before field sessions.

---

## Next session — recommended work

> **Standing goal:** reduce Pi resource usage as much as possible before and after every feature. Check DIAG tab (cpu_percent, temperature, process_cpu_percent) before and after each change.

**Primary blocker: powerbank** — Viking PN-964PD shuts off without USB load. Options:
- USB keep-alive dongle (small device in spare USB port drawing minimal current)
- Viking pass-through cable (charger → IN, Pi → OUT) for tethered test1 / M1
- Virtual USB dongle service (`rover2-virtual-usb-dongle.service`) — CPU cost ~12%

**Once powerbank resolved:** run tethered validation T0–T4 → M1 mobility → tag v1.0.0.

**Software options (no hardware needed):**
- Test and verify AI agent with more complex multi-step queries
- "ME only" follow: BLE gate (only follow if BLE AND camera both agree on same person) + target lock so robot doesn't switch targets mid-follow — deferred from previous session
- Activity timeline: log follow events, AI queries, alerts to SQLite with timestamps; show as audit trail in TOOLS tab
- One-click health report: downloadable `.txt` report of last 24h
- Export metrics as CSV from METRICS tab

**Agent tuning** (if answers are wrong or slow):
- Check `ollama list` on Pi — confirm llama3.2:1b is present
- Check `journalctl -u rover2-api -n 50` for agent tool errors
- Reduce `_MAX_TOOL_ROUNDS` if still too slow; increase if complex queries need more rounds

**Tuning knobs if needed:**
```yaml
body_tracker:
  turn_speed: 100        # lower (80) if turns overshoot
  forward_speed: 120     # lower (90) if advance too fast
  target_bbox_width: 0.35  # higher (0.45) to stop farther away
  centre_zone: 0.30      # higher (0.40) for looser centering
  confidence: 0.40       # lower (0.30) if person not detected reliably
```

### Suggested Claude Code prompt

```
Read HANDOFF.md in full before making changes.

Continue ROVER2 development (eth 192.168.70.11:8082).
Current status: AI agent + VLM + metrics + alerts all working.
Primary blocker: Viking powerbank auto-shutoff prevents untethered test1.

Options:
- Resolve powerbank issue (virtual USB dongle or keep-alive hardware)
- Test AI agent features (CHAT tab presets, voice, METRICS "ASK AI")
- Software features: activity timeline, ME-only follow, health report export
- Tethered validation T0–T4 once ready
```

---

## Versioning

**Repo:** https://github.com/NotAmbassad0r/rover2

**Branches:**
- `main` — stable, tagged releases only
- `dev` — active development (you are here)

| Tag | Meaning |
|-----|---------|
| `v0.3.0` | Phase 3 follow enabled — dev phase |
| `v0.5.0` | Phase 5 AI agent + VLM + metrics + alerts — dev phase |
| `v1.0.0` | Phase 3+4+5 verified untethered — test1 pass → merge dev→main |

**Workflow:**
```bash
git checkout dev
git add -p && git commit -m "feat: ..."
git push

# test1 milestone
git checkout main && git merge --no-ff dev
git tag -a v1.0.0 -m "Phase 3+4+5 verified untethered (test1)"
git push origin main v1.0.0
gh release create v1.0.0 --generate-notes
```

---

## Standing rules (this repo)

- Prefer changes in `rover2/` unless explicitly merging into v1
- After Pi code changes: `./deploy_pi.sh` (targets **192.168.70.11** by default)
- After firmware changes: `./scripts/flash_firmware.sh`
- Do **not** set global `PYTHONPATH` to system site-packages on rover2-api
- Re-run `scripts/link_hailo_for_rover2.sh` on Pi if venv was recreated
- Port **8082** for ROVER2; do not change v1 **8080** without coordination
- Only one of `rover-api` / `rover2-api` may use `/dev/ttyUSB0`
- `hailo-ollama` must remain disabled while ROVER2 is in use
- Agent model must be `llama3.2:1b` — gemma2:2b does not support tool calls in Ollama

---

## Resource efficiency (standing goal)

**Reduce Pi resource usage as much as possible.** The Pi 5 runs hot (cpu_thermal regularly 70–80°C) and powers the Hailo chip, serial bridge, BLE scan, Ollama LLM, and web API simultaneously.

| Area | Rule |
|------|------|
| **Hailo** | Offload all inference to the AI HAT. Never replicate on CPU what Hailo can do. |
| **Inference loop** | Run Hailo only when DETECT or FOLLOW is active — idle when off. |
| **VLM** | Lazy-loaded; shares Hailo chip via ROUND_ROBIN — no extra resource cost at idle. |
| **Ollama** | Called only when agent receives a question that bypasses fast-path. Service stays loaded but idle is ~0% CPU. |
| **Agent fast-path** | 9 patterns answer without Ollama (regex → tool → format). Always prefer fast-path over LLM. |
| **BLE scan** | Active scan at minimum viable interval. |
| **Metrics collection** | Default 5 s interval. Do not decrease below 5 s. |
| **WebSocket telemetry** | Default 400 ms. Do not push faster unless a feature requires it. |
| **New features** | Before merging: check DIAG tab — cpu_percent, process_cpu_percent, temperature. If idle CPU rises >5%, profile and optimise first. |
| **Python overhead** | Prefer asyncio over threads where possible. Avoid polling loops; use event-driven callbacks. |
| **Memory** | rover2-api target: <150 MB RSS. |
