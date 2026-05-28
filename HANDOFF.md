# HANDOFF.md — ROVER2

Last updated: 2026-05-28 (local wake-word STT via faster-whisper, BLE disable option)

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

### WiFi down after hard power cut

FAT32 boot partition has no journaling — a hard power cut can corrupt `/boot/firmware/cmdline.txt` (leaves it empty). This causes the Pi to fail to boot or fail to connect WiFi.

**Fix — connect SSD to laptop (`/dev/sda`):**
```bash
sudo bash /tmp/fix-cmdline3.sh   # see scripts below
sudo bash /tmp/fix-nm-state.sh   # clear NM state if WiFi still not connecting
```

Known-good cmdline.txt content:
```
console=serial0,115200 multipath=off dwc_otg.lpm_enable=0 console=tty1 root=PARTUUID=a6f9ddfb-02 rootfstype=ext4 rootwait fixrtc cfg80211.ieee80211_regdom=GB
```

**Prevention:** Boot partition is now mounted **read-only** in `/etc/fstab` (`ro,defaults`). This prevents any process from writing to the FAT32 partition at runtime, eliminating the corruption risk. If you need to update boot files (e.g. `config.txt`), remount rw temporarily:
```bash
sudo mount -o remount,rw /boot/firmware
# make changes
sudo mount -o remount,ro /boot/firmware
```

NM credentials: WiFi keyfile at `/etc/NetworkManager/system-connections/GUCZ-744.nmconnection`. If WiFi connects but to wrong IP, clear NM state: `rm -f /var/lib/NetworkManager/NetworkManager.state timestamps seen-bssids`.

---

## Phase status

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 0 | Motors, gripper, ultrasonic, web D-pad | **Done** |
| 1 | Forward obstacle stop (ultrasonic < 40 cm) | **Verified** |
| 2 | WebSocket D-pad, live telemetry, stop on disconnect | **Done** |
| 2b | Arm lift (PORT3B), web + API | **Done** (firmware 1.0.3) |
| 3 | Camera / Hailo person follow (FOLLOW toggle) | **Done — verified on robot** |
| 4 | BLE beacon fallback follow (Z Flip 6 via fcf1 UUID) | **Done — verified** |
| 5 | Web TOOLS + DIAG + CHAT + METRICS + AI Agent + VLM + Alerts | **Done** |
| test1 | Follow mobility (M1: untethered WiFi + battery) | **Pending** — blocked on USB cable (5A cable ordered) |
| v1.0.0 | M1 pass → merge dev→main | Pending M1 |

---

## Power situation (critical — read before battery testing)

**Viking PN-964PD bank provides 15W max** (5V/3A). Pi 5 + Hailo inference needs ~18–20W sustained.

Root cause confirmed via PMIC ADC (`vcgencmd pmic_read_adc`):
- `EXT5V_V = 4.89V` (drooping from 5V → bank at current ceiling)
- `VDD_CORE_A = 7.95A` during Hailo model init (CPU at peak)
- Pi cuts off after ~46 s of sustained inference

**Why 15W:** The Viking bank supports PD, but the USB-C cable limits negotiation to 3A (no e-marker chip). Even if the bank offers 5V/5A, a standard USB-C cable caps at 3A = 15W. Pi 5 with `usb_max_current_enable=1` requests 5A but can't get it without a 5A-rated cable.

**Fix in progress:** 5A/100W e-marked USB-C cable ordered. Until it arrives, keep Pi on mains.

**Software mitigations already in place:**
- `arm_freq=1800` in `/boot/firmware/config.txt` (was `arm_boost=1` @ 2400MHz) — saves ~2W
- Lazy Hailo init — model loads only when DETECT/FOLLOW first enabled, not at service start
- 30s warmup delay before Hailo load (CPU settles post-boot)
- `frame_interval_s: 0.25` (4fps) — reduces sustained Hailo draw
- `EXT5V_V` and `VDD_CORE_A` logged to journal at `[pre/post-hailo-load]` for diagnosis

---

## Architecture

```
Browser (index.html)  https://192.168.70.11:8082/
    → WebSocket wss://<pi>:8082/ws       drive, grip, arm, tracking, telemetry, alerts
    → HTTPS REST /api/*                  drive, diagnostics, scripts, WiFi, AI agent, metrics
    → Camera preview                     http://<pi>:8081/stream  (rover-camera.service)

ROVER Face PWA (face/index.html) — Samsung Galaxy A32
    VAD wake mode:     AnalyserNode RMS poll (80 ms) → energy detected →
                       MediaRecorder 2.5 s chunk → POST /api/voice/wake → faster-whisper
                       → wake=true → _startConversation()
    Conversation mode: MediaRecorder + RMS silence gate → POST /api/voice/transcribe →
                       faster-whisper → POST /api/voice/converse → hailo-ollama/llama3.2:3b
                       → Web Speech API TTS reply

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
    pi/voice_engine.py  Piper TTS (binary subprocess) + faster-whisper STT + ROVER personality
    pi/audio_router.py  UDP audio router (asyncio, zero-CPU idle) + /ws/audio WebSocket bridge

    ── ROVER Face PWA ────────────────────────────────────────────────────────────
    face/index.html     Samsung Galaxy A32 voxel face (canvas, 8 states, fullscreen portrait)
    face/manifest.json  PWA manifest (add to home screen)
    face/sw.js          Cache-first service worker

    ── Piper TTS binary ──────────────────────────────────────────────────────────
    /opt/rover2/piper/piper            Statically-linked piper binary (RPATH=$ORIGIN)
    /opt/rover2/voices/en_GB-cori-high.onnx   British English voice model (preferred)
    /opt/rover2/voices/en_GB-alan-medium.onnx  Fallback voice (still present)

    ── Hailo AI HAT+ 2 ───────────────────────────────────────────────────────
    YOLOv8m_h10.hef     Body tracker (FOLLOW / DETECT)  group_id=rover2
    Qwen2-VL-2B.hef     VLM scene description           group_id=rover2
    ← ROUND_ROBIN scheduler: both models share the chip without mode-switching

    ── Ollama (system service on Pi) ─────────────────────────────────────────
    llama3.2:1b         Agent LLM (tool-use capable; gemma2:2b does NOT support tools)
    gemma2:2b           Available but NOT used for tool-use
    deepseek-r1:1.5b    Available
```

| Service | Port | Notes |
|---------|------|--------|
| `rover2-api.service` | **8082** | ROVER2 control + follow + AI |
| `rover-camera.service` | **8081** | MJPEG (v1 stack; shared) |
| `ollama.service` | **11434** | Local LLM — llama3.2:1b for agent tool-use |
| `rover-api` (v1) | 8080 | Stop when testing ROVER2 serial |
| `hailo-ollama` | — | **Permanently disabled** — conflicts with Hailo chip ownership |
| `rover2-restore-wifi.service` | — | Boot: re-apply WiFi from `/boot/firmware/network-config` |
| `rover2-virtual-usb-dongle.service` | — | Optional ~12% CPU keep-alive for Viking bank |

---

## What works (confirmed 2026-05-26)

- D-pad with arc turns (`left: [1,0]`, `right: [0,-1]`) — single wheel turns, works on hard floor; left/right direction corrected 2026-05-26
- Gripper open/close (PORT4B)
- **Arm lift** hold up/down + nudge pulse (PORT3B; firmware 1.0.3)
- Ultrasonic on **PORT_8** (firmware auto-scan)
- Safety: forward blocked when distance < 40 cm; FOLLOW steers around
- WebSocket heartbeat: 10s timeout (tolerates WiFi jitter)
- **Live camera** in UI (loads `:8081/stream`)
- **Camera FOLLOW** — Hailo YOLOv8m tracks person, LEFT/RIGHT/FWD/HOLD; confirmed working on robot
- **Lazy Hailo init** — model loads only on first DETECT/FOLLOW enable; 30s warmup delay post-boot
- **BLE fallback follow** — when camera loses person >2 s, rover rotates to re-acquire via RSSI gradient
- **BLE follow toggle** — UI BLE button (blue) enables/disables BLE fallback independently
- **UI mode buttons** — DETECT (amber), FOLLOW (green), BLE (blue) — visual toggle state
- **UI SOURCE row** — shows CAMERA / BLE (dBm) / SEARCHING… in real time
- PMIC power logging at Hailo load (`[pre/post-hailo-load]` in journal)
- Boot partition read-only (`/etc/fstab`: `ro,defaults`) — cmdline.txt protected from corruption
- `hailo-ollama.service` permanently disabled
- **ROVER Face PWA** — `/face/` served by rover2-api; voxel face canvas, 8 states, WebSocket telemetry
- **TTS** — piper binary (`/opt/rover2/piper/piper`) + **en_GB-cori-high** voice; `POST /api/voice/speak` streams WAV; length_scale 1.05; for proactive server events only. Agent replies spoken via **Web Speech API** on A32 (`speechSynthesis`, British voice, rate 0.88)
- **STT** — faster-whisper tiny (int8, CPU, ctranslate2, ~150 MB RSS on first call). `/api/voice/transcribe` (full turn), `/api/voice/wake` (2.5 s chunk, returns `{wake: bool, transcript: str}`). No cloud, fully offline
- **Proactive speech** — voice_engine.py fires BOOT_COMPLETE, PERSON_FOUND/LOST, OBSTACLE, THERMAL events (debounced 30s, ROVER_A32 mode only)
- **Audio routing** — `POST /api/audio/route {mode: ROVER_A32|BUDS}`; `GET /api/status` includes `audio_mode`
- **`/ws/audio`** WebSocket — binary PCM frames from TTS → browser Web Audio API playback
- **HTTPS** — rover2-api runs with self-signed TLS cert; certs at `/opt/rover2/rover.key` + `rover.crt`; UI auto-selects `wss://`; microphone (getUserMedia) works without chrome://flags on HTTPS
- **RSS with TTS** — 88 MB base (piper runs as subprocess, zero persistent RSS)
- **Web TOOLS tab** — diagnostics, script catalog, follow tuning sliders, RESTORE WIFI
- **Web DIAG tab** — full system diagnostics with charts
- **Web CHAT tab** — NL commands, VLM, AI agent, 16 presets, voice
- **Web METRICS tab** — Chart.js graphs (CPU%, Temp, RAM%)
- **AI Agent** — Ollama llama3.2:1b + 20 tools; 9 fast-path patterns
- **VLM scene description** — Hailo Qwen2-VL snapshot caption

---

## AI / LLM system

### On-device AI stack (fully offline — no cloud, no data egress)

```
Browser voice/text
    → /api/chat             chat_router.py  → direct NL commands (0 ms LLM)
    → /api/agent/chat       agent.py        → fast-path (<1 s) or Ollama (10–40 s)
    → /api/vision/describe  vlm_engine.py   → Hailo VLM snapshot caption

A32 voice pipeline (fully local — no cloud)
    Wake word:   AnalyserNode RMS → MediaRecorder 2.5 s → POST /api/voice/wake
                 → faster-whisper → "rover" detected → conversation start
    Conversation: MediaRecorder + VAD silence gate → POST /api/voice/transcribe
                 → POST /api/voice/converse → hailo-ollama (fast) or llama3.2:3b (complex)
                 → Web Speech API TTS (on-device, British voice)
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

**Adding new tools:**
1. Add definition to `_READ_TOOLS` or `_DANGEROUS_TOOLS` in `agent.py`
2. Add case to `_run_tool()`
3. Optionally add fast-path entry to `_FAST_PATTERNS`

**Limits:** `_MAX_TOOL_ROUNDS = 4`, `_OLLAMA_TIMEOUT = 60 s`

---

## Follow behaviour

**Design intent:**
1. **Camera** — see the operator and follow in frame (primary).
2. **BLE** — when not in frame, home on Z Flip 6 `fcf1` RSSI (secondary; togglable).
3. **Obstacles** — forward ultrasonic < 40 cm: steer, do not ram.

**Camera follow** (primary):
- Hailo YOLOv8m NMS output — detection-major format `(80, 100, 5)` — see `body_tracker_parse.py`
- Person centred: HOLD; left of centre: LEFT turn; right: RIGHT turn; too far: FWD
- `turn_speed: 210`, `forward_speed: 170`, `confidence: 0.40`, `centre_zone: 0.30`, `frame_interval_s: 0.25`

**Lazy Hailo init + warmup:**
- Hailo model loads only on first DETECT/FOLLOW enable (not at service start)
- `hailo_warmup_s: 30.0` — enforces 30s delay after service start before Hailo will load
- Prevents CPU+Hailo concurrent spike during boot
- `[pre/post-hailo-load]` PMIC readings logged at model load time

**BLE fallback** (secondary — auto-activates when camera loses person):
- 2 s grace period after camera LOST before BLE activates
- Rotates slowly (speed 50), watches RSSI trend — flips direction if signal worsens
- Advances (speed 60) if RSSI > -75 dBm; holds if RSSI > -60 dBm (very close)
- Camera re-acquiring immediately cancels BLE and resumes camera follow
- **BLE toggle:** `POST /api/tracking {"ble_follow_enabled": false}` disables fallback
- UI: BLE button (blue = enabled, dim = disabled)

**BLE device matching (Z Flip 6):**
- Matches by service UUID `0000fcf1-0000-1000-8000-00805f9b34fb` (Samsung service)
- NOT by MAC (Android randomises BLE MAC) or name
- Config: `ble_tracker.beacon_uuid` in `pi/config.yaml`
- Note: Samsung UUID `fcf1` is common in dense apartments — many neighbour devices may appear

**Arc turns (hard floor):**
- `direction_map: left: [1, 0], right: [0, -1]` — one wheel rolls, other stops (corrected 2026-05-26)
- Avoids pivot-turn floor friction (both wheels scrubbing = stalls on smooth floor)

**Ultrasonic safety + follow avoidance:**
- `SafetyMonitor` hard-blocks forward < 40 cm
- During FOLLOW, `steer_around_obstacle()` replaces blocked FWD with a turn

---

## Hardware ports (MegaPi)

| Function | Port |
|----------|------|
| Left drive | PORT1B |
| Right drive | PORT2B |
| **Arm lift** | **PORT3B** |
| Gripper open/close | PORT4B |
| Ultrasonic | Auto-scan PORT_1–8 (locked on first valid echo; usually PORT_8) |

If arm moves the wrong way: `arm.invert: true` in `pi/config.yaml`.

---

## Firmware

| Item | Value |
|------|--------|
| Sketch | `firmware/rover2_basic/rover2_basic.ino` |
| Version | **`rover2-basic-1.0.3`** (`motors: 3` in ready event) |
| Arm commands | `{"cmd":"arm","speed":N}` (−255…255); `{"cmd":"arm","action":"up"|"down"}` = ~800 ms pulse |
| Ultrasonic | `distanceCm(400)` — readings ≥400 = no echo |

---

## Deploy

```bash
cd ~/Documents/projects/rover2
./deploy_pi.sh
```

- Auto-picks first reachable host: `rover-eth`, `192.168.70.11`, WiFi, Tailscale.
- Disables `hailo-ollama.service` permanently.
- Runs `scripts/link_hailo_for_rover2.sh` on Pi.
- Sideloads `httpx` and `bleak` if pip fails.

```bash
ROVER2_SKIP_VIRTUAL_DONGLE=1 ./deploy_pi.sh   # code-only, skip power profile
```

Logs:
```bash
ssh ambassad0r@192.168.250.254 "journalctl -u rover2-api -f"
```

Verify:
```bash
ssh ambassad0r@192.168.250.254 "curl -sk https://localhost:8082/api/status | python3 -m json.tool"
# Expect: tracking_available true, tracking_hailo_ready true (only after DETECT/FOLLOW first enabled),
#         ble_available true, ble_seen true, ble_follow_enabled true
# Note: -k flag needed for curl with self-signed cert
```

---

## Web UI

**https://192.168.250.254:8082/** (WiFi) or **https://192.168.70.11:8082/** (eth)

> **HTTPS only** — rover2-api runs with a self-signed TLS cert. Use `https://`. First visit: accept the cert warning (Advanced → Proceed). The UI auto-selects `wss://` for WebSocket when served over HTTPS.

**Hard-refresh after deploy:** `Ctrl+Shift+R`

### CONTROL tab

| Section | Notes |
|---------|--------|
| STATUS | WEBSOCKET, SERIAL, ULTRASONIC, SAFETY, FOLLOW, **SOURCE**, PERSON, BLE BEACON, UPTIME |
| SOURCE row | CAMERA (green) / BLE (dBm, orange) / SEARCHING… / — |
| Mode buttons | **DETECT** (amber), **FOLLOW** (green), **BLE** (blue) — dim when off |
| LIVE CAMERA | MJPEG from port **8081** |
| ARM LIFT | Hold ▲/▼; NUDGE = short pulse |
| DRIVE | D-pad; disabled while FOLLOW on |
| GRIP / E-STOP | Gripper + emergency stop |

**Follow mode selector:** `DETECT | CAMERA | FUSED | BLE` — four buttons replace the old FOLLOW+BLE pair:
- **CAMERA** (green) — camera-only follow, no BLE fallback
- **FUSED** (teal) — camera primary + BLE fallback when camera loses person (default)
- **BLE** (blue) — BLE-only follow, Hailo not used, zero camera inference power
- Clicking the active mode button turns follow OFF.

### TOOLS / DIAG / CHAT / METRICS tabs

Unchanged from previous session — see prior HANDOFF or the UI itself.

---

## WebSocket protocol

Endpoint: `ws://192.168.250.254:8082/ws`

**Client → server:** `drive`, `stop`, `grip`, `arm`, `arm_pulse`, `tracking`, `ping`
**Server → client:** `telemetry`, `ack`, `pong`, `error`

Telemetry fields include: `tracking_enabled`, `tracking_detect_only`, `tracking_hailo_ready`, `person_detected`, `ble_active`, `ble_follow_enabled`, `ble_available`, `ble_seen`, `ble_rssi`, `alerts`.

Tracking API: `POST /api/tracking`
- `{"enabled": true}` — enable FOLLOW
- `{"detect_only": true}` — DETECT only
- `{"enabled": false}` — disable all
- `{"ble_follow_enabled": false}` — disable BLE fallback (can be combined with other fields)

---

## Config highlights (`pi/config.yaml`)

```yaml
drive:
  default_speed: 120
  direction_map:          # arc turns — one wheel, avoids hard-floor friction
    forward: [1, -1]
    back: [-1, 1]
    left: [1, 0]          # left motor only — fixed 2026-05-26 (was inverted)
    right: [0, -1]        # right motor only — fixed 2026-05-26 (was inverted)

websocket:
  heartbeat_timeout_s: 10.0   # 10s tolerates WiFi jitter (was 4s)

body_tracker:
  turn_speed: 210              # raised for hard floor (was 100)
  forward_speed: 170           # raised (was 120)
  frame_interval_s: 0.25       # 4fps — reduces Hailo power draw on battery
  hailo_warmup_s: 30.0         # delay before Hailo loads — lets CPU idle after boot
  confidence: 0.40
  centre_zone: 0.30
  target_bbox_width: 0.35
  ble_follow_enabled: true     # set false to disable BLE fallback

ble_tracker:
  enabled: true        # set false for office demo (saves CPU + heat — no BLE needed)
                       # set true for home use (guard mode arm/disarm via beacon)
  beacon_uuid: "0000fcf1-0000-1000-8000-00805f9b34fb"   # Samsung Z Flip 6
  device_name: "-Lars's Z flip 6"
  device_mac: "F0:05:1B:0A:E0:4C"
```

---

## Hailo / camera follow

| Item | Location |
|------|----------|
| YOLO Model | `/opt/rover/models/yolov8m_h10.hef` |
| VLM Model | `/opt/rover/models/Qwen2-VL-2B-Instruct.hef` |
| YOLO code | `pi/body_tracker.py`, `pi/body_tracker_parse.py` |
| VLM code | `pi/vlm_engine.py` |
| NMS format | Detection-major `(80, 100, 5)` — field order: `score, y0, x0, y1, x1` |
| Chip sharing | `group_id="rover2"` + `ROUND_ROBIN` |

**Pi boot config (`/boot/firmware/config.txt`):**
- `usb_max_current_enable=1` — requests max USB current via PD
- `arm_freq=1800` — CPU capped at 1800MHz (was arm_boost=1 @ 2400MHz), saves ~2W
- `country_code=GB` — WiFi regulatory domain

---

## Known issues / notes

1. **USB cable 15W ceiling** — Viking bank + standard USB-C cable = 5V/3A = 15W. Pi+Hailo needs ~18–20W. Cutoff after ~46s inference. **Fix: 5A/100W e-marked cable ordered.** Until then, use mains.
2. **cmdline.txt corruption on hard power cut** — FAT32 boot partition, no journaling. Fixed by mounting boot partition read-only. If it happens again, connect SSD to laptop and restore cmdline.txt manually (PARTUUID=a6f9ddfb-02).
3. **NM state corruption on hard power cut** — WiFi won't connect. Fix: connect SSD, `rm /var/lib/NetworkManager/NetworkManager.state timestamps seen-bssids`, reboot.
4. **BLE false positives** — Samsung UUID `fcf1` is common. In a dense apartment many devices advertise it. Disable BLE (UI button) if false positives cause unwanted motion during camera testing.
5. **Hailo first-enable delay** — Hailo loads on first DETECT/FOLLOW enable with 30s post-boot warmup. First enable after boot may take a few seconds before inference starts.
6. **llama3.2:1b for agent** — gemma2:2b does NOT support tool-use in Ollama. Do not switch.
7. **hailo-ollama permanently disabled** — conflicts with Hailo chip. Do not re-enable.
8. **Pi TCP/443 blocked at gateway** — pip fails for external packages. deploy_pi.sh sideloads wheels.
9. **BLE MAC randomisation** — Android 10+ randomises BLE MAC every ~15 min. Always match by UUID.
10. **Browser cache** — Hard-refresh (`Ctrl+Shift+R`) after every deploy.
11. **piper-phonemize has no cp313 wheel** — Python piper-tts API unusable on Pi's Python 3.13. Solution: use `piper` binary at `/opt/rover2/piper/piper` (statically linked, installed by `deploy_pi.sh`). voice_engine.py calls it as a subprocess.
12. **voices/ excluded from rsync** — `deploy_pi.sh` uses `--exclude 'voices/'` to protect downloaded models from `--delete`. Voice model lives at `/opt/rover2/voices/` on Pi only.
13. **STT memory cost** — `openai-whisper` depends on `torch` (~800 MB RSS when loaded). Once transcribe() is called, rover2-api RSS spikes from ~80 MB to ~912 MB. The memory watchdog correctly fires critical alerts. Whisper is lazy-loaded (only on first transcribe call). Restart rover2-api to recover memory. Long-term fix: switch to `faster-whisper` (ctranslate2, ~150 MB RSS) — needs ctranslate2/av aarch64 wheels sideloaded. STT endpoint (`/api/voice/transcribe`) now returns valid JSON always — never 500.
    **faster-whisper is the STT backend** — `openai-whisper`/`torch` are NOT used. faster-whisper tiny (int8, CPU, ~150 MB RSS) transcribes audio via `/api/voice/transcribe` and `/api/voice/wake`. `webkitSpeechRecognition` was removed (requires Google servers — breaks offline demo). Wake word and conversation audio is now captured on A32 via `MediaRecorder` + `AnalyserNode` RMS VAD and POSTed to the Pi.
14. **Whisper import path** — `openai-whisper` is in `/home/ambassad0r/.local/lib/python3.13/site-packages/` (installed with `pip install --user`). `tqdm` and `torch` are in `/usr/lib/python3/dist-packages/` (apt/system pip). `voice_engine._load_whisper()` adds both paths to `sys.path` before `import whisper`.
15. **Face PWA on A32** — server now runs HTTPS, so getUserMedia works without `chrome://flags`. Accept the self-signed cert warning once on first visit to `https://192.168.250.254:8082/`.
16. **HTTPS self-signed cert** — Browsers warn on first visit. Accept once (Advanced → Proceed). Curl on Pi needs `-k` flag. Cert files: `/opt/rover2/rover.key` + `rover.crt` (owned root:ambassad0r, mode 640). Not in repo — regenerate with `openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -keyout rover.key -out rover.crt -days 3650 -nodes -subj "/CN=rover"` if lost.
17. **Left/right direction was inverted** — Fixed 2026-05-26 by swapping direction_map in config.yaml: `left: [1,0]`, `right: [0,-1]`. Root cause: PORT1B is wired to the physical right motor, so firmware's "left motor" is actually the right wheel. D-pad, follow, BLE turns, and obstacle avoidance all fixed by this single config change.
18. **MegaPi on AA batteries** — Brand new AAs may not provide enough current for arc turns at speed 210. If motors stall, use MegaPi mains or reduce turn_speed.

---

## Testing

**Plan:** `docs/TEST_PLAN.md` | **Results:** `docs/TEST_RESULTS.md`

| Test | Status |
|------|--------|
| T0 — Preflight | **PASS** |
| T1.1 — Hailo available | **PASS** |
| T1.2 — Person detect | **PASS** |
| T1.3–T1.7 — Follow behaviour | Re-test required — left/right direction was inverted until 2026-05-26 fix |
| T2 — Drive / arm / safety | Not formally signed off |
| T3 — BLE fallback | BLE auto-activates confirmed; UI toggle confirmed |
| T4 — Sign-off | Pending |
| U0 — Untethered (battery) | **Blocked** — 5A cable pending |

```bash
./scripts/run_local_tests.sh              # dev PC — unit tests
./scripts/check_rover_ready.sh            # Pi preflight
```

---

## Next session — recommended work

> **Standing goal:** reduce Pi resource usage as much as possible before and after every feature.

**Immediate (when 5A cable arrives):**
- U0: boot Pi on bank, let it stabilise 60s, enable DETECT then FOLLOW — should survive
- If stable: walk around room, log M1 mobility test results in TEST_RESULTS.md
- Tag v0.5.0 after U0 passes; merge dev→main + tag v1.0.0 after M1 passes

**In the meantime (mains, WiFi):**
- Complete T1.3–T1.7 formal sign-off (follow, turn, advance, obstacle steer)
- Complete T2 (D-pad, stop, gripper, arm, ultrasonic, forward block)
- Complete T3 (BLE beacon seen, handoff, reacquire)
- Log all results in TEST_RESULTS.md

**Voice assistant (now fully local — no cloud):**
- Wake word: say "rover" → A32 VAD detects energy → 2.5 s chunk → faster-whisper on Pi → conversation starts
- Conversation: MediaRecorder + RMS silence gate → transcribe → hailo-ollama or llama3.2:3b → Web Speech TTS
- To test offline: enable airplane mode on A32, visit `https://192.168.250.254:8082/face/`, say "rover"
- MIC button manually starts/ends conversation (no wake word needed)
- For office demo with no BLE: set `ble_tracker.enabled: false` in config.yaml → deploy

**Software options:**
- "ME only" follow: BLE + camera must agree before following (prevents false positives in dense BLE environments)
- Activity timeline: log follow events to SQLite; show in TOOLS
- Tune `WAKE_RMS_THRESHOLD` (0.015) in face/index.html if false wakes or misses in noisy environments

**Tuning knobs:**
```yaml
body_tracker:
  turn_speed: 210        # lower if turns overshoot
  forward_speed: 170     # lower if advance too fast
  target_bbox_width: 0.35  # higher (0.45) to stop farther away
  centre_zone: 0.30      # higher (0.40) for looser centering
  confidence: 0.40       # lower (0.30) if person not detected reliably
  hailo_warmup_s: 30.0   # reduce after 5A cable — less need to wait
```

### Suggested Claude Code prompt

```
Read HANDOFF.md in full before making changes.

Continue ROVER2 development. Pi at 192.168.250.254 (WiFi) or 192.168.70.11 (eth).
API is HTTPS — use https:// and curl -k. WebSocket is wss://.
Current status: follow works on mains; left/right direction corrected 2026-05-26.
Battery test blocked by USB cable (5A cable ordered).
Boot partition is read-only — if you need to update /boot/firmware files, remount rw first.

Immediate priority: complete T1–T3 formal tests on mains, log in TEST_RESULTS.md.
Then: U0 battery test when 5A cable arrives.
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
| `v0.5.0` | Phase 5 AI agent + VLM + metrics + alerts — dev phase (pending tag) |
| `v1.0.0` | Verified untethered follow (M1 pass) → merge dev→main |

---

## Standing rules (this repo)

- After Pi code changes: `./deploy_pi.sh` (targets **192.168.70.11** by default) or rsync manually
- After firmware changes: `./scripts/flash_firmware.sh`
- Do **not** set global `PYTHONPATH` to system site-packages on rover2-api
- Port **8082** for ROVER2; do not change v1 **8080** without coordination
- Only one of `rover-api` / `rover2-api` may use `/dev/ttyUSB0`
- `hailo-ollama` must remain disabled while ROVER2 is in use
- Agent model must be `llama3.2:1b` — gemma2:2b does not support tool calls in Ollama
- Boot partition is read-only — remount rw before editing `/boot/firmware/` files

---

## Resource efficiency (standing goal)

| Area | Rule |
|------|------|
| **Hailo** | Offload all inference to the AI HAT. Never replicate on CPU. |
| **Inference loop** | Run Hailo only when DETECT or FOLLOW is active. Lazy init + warmup. |
| **VLM** | Lazy-loaded; shares Hailo chip via ROUND_ROBIN. |
| **Ollama** | Called only when agent bypasses fast-path. |
| **BLE scan** | Active scan at minimum viable interval. |
| **Metrics collection** | Default 5 s interval. Do not decrease below 5 s. |
| **WebSocket telemetry** | Default 400 ms. Do not push faster. |
| **New features** | Before merging: check DIAG tab — cpu_percent, temperature. |
| **Memory** | rover2-api target: <150 MB RSS. |
