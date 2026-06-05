# HANDOFF.md — ROVER2

Last updated: 2026-06-05 (HailoRT 5.2.0 upgrade; VLM working; hailo-ollama disabled — see issue #26; backlog at docs/BACKLOG.md)

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
| Dev machine | central-computer — <CENTRAL_IP> (Kubuntu 26.04) |
| Dev repo | `~/Documents/projects/rover2/` |
| Pi deploy path | `/opt/rover2/` |
| Pi hostname | `rover` (Pi 5 8GB + AI HAT+ 2; same host as v1) |

### Pi network

| Interface | IP | Notes |
|-----------|-----|-------|
| eth0 | **<ROVER_ETH_IP>** | Preferred — use for deploy, UI, SSH |
| wlan0 | <ROVER_WIFI_IP> | Home/office network (stays connected) |
| wlan1 (AP) | **10.0.0.1** | ROVER2 AP — always-on, SSID: ROVER2 |
| Tailscale | <ROVER_TAILSCALE_IP> | Remote access |

**Dev machine has no route to <ROVER_WIFI_IP>** — deploy always via eth0 (rover-eth). Use WiFi IP only from a device on the same WiFi (phone, laptop on local network).

**ROVER2 WiFi AP (wlan1 — RTL8812AU dongle):**
- SSID: `ROVER2` · Password: `6nat0n6cNZQ5Q5` · Band: 2.4 GHz ch 6
- Pi IP on AP network: `10.0.0.1` · DHCP range: `10.0.0.10–10.0.0.50`
- Web UI from AP: **`https://10.0.0.1:8082/`**
- DNS: `rover.local` → `10.0.0.1` (via dnsmasq on wlan1)
- Services: `hostapd` + `dnsmasq` + `rover2-wlan1-ip.service` (static IP oneshot)
- wlan0 and wlan1 run independently — AP does not affect home WiFi

### SSH

```bash
ssh ambassad0r@<ROVER_ETH_IP>       # eth0 (preferred)
ssh rover-eth                      # ~/.ssh/config → <ROVER_ETH_IP>
ssh ambassad0r@<ROVER_WIFI_IP>     # WiFi (from same-subnet device only)
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
| test1 | Follow mobility (M1: untethered WiFi + battery) | **Done — verified untethered 2026-06-05** |
| v1.0.0 | M1 pass → merge dev→main | **Done — tagged 2026-06-05** |

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
- `frame_interval_s: 1.0` (1fps) — reduces sustained Hailo draw (was 0.25 / 4fps)
- TurboJPEG decode at 1/2 scale (320×240) — replaces cv2.imdecode; eliminates cvtColor
- Stream-skip sleep: wait for remainder of interval instead of spinning on the camera stream
- `EXT5V_V` and `VDD_CORE_A` logged to journal at `[pre/post-hailo-load]` for diagnosis

---

## Architecture

```
Browser (index.html)  https://<ROVER_ETH_IP>:8082/
    → WebSocket wss://<pi>:8082/ws       drive, grip, arm, tracking, telemetry, alerts
    → HTTPS REST /api/*                  drive, diagnostics, scripts, WiFi, AI agent, metrics
    → Camera preview                     http://<pi>:8081/stream  (rover-camera.service)

ROVER Face PWA (face/index.html) — Samsung Galaxy A32
    VAD wake mode:     AnalyserNode RMS poll (80 ms) → energy detected →
                       MediaRecorder 2.5 s chunk → POST /api/voice/wake → faster-whisper
                       → wake=true → _startConversation()
    Conversation mode: MediaRecorder + RMS silence gate → POST /api/voice/transcribe →
                       faster-whisper → POST /api/voice/converse → run_spoken_turn() agentic
                       → Web Speech API TTS reply

    ── Python services (pi/) ──────────────────────────────────────────────────
    pi/main.py               Entrypoint: wires all components, starts uvicorn
    pi/server.py             FastAPI: 50+ REST endpoints + WebSocket hub
    pi/ws_control.py         WebSocket hub; telemetry push with extra fields (alerts)
    pi/safety.py             Ultrasonic forward gate
    pi/body_tracker.py       Hailo YOLOv8m person follow + BLE fallback + obstacle steer
    pi/body_tracker_parse.py YOLO NMS output parser (detection-major)
    pi/follow_nav.py         Obstacle steer + BLE RSSI homing helpers
    pi/ble_tracker.py        BLE RSSI beacon scanner (bleak)
    pi/megapi.py             pyserial, ultrasonic poll
    pi/arm_control.py        Arm PWM helper (PORT3B hold-to-move + pulse)
    pi/camera_proxy.py       MJPEG proxy from rover-camera through rover2-api
    pi/camera_idle.py        Ultrasonic-triggered camera idle sleep (stops proxy pull)
    pi/guard.py              Door guard: BLE arm/disarm + Hailo intruder detect + HA webhooks
    pi/thermal.py            Thermal monitor: vcgencmd, auto CPU throttle at 80°C
    pi/diagnostics.py        gather_diagnostics(), SCRIPT_CATALOG, WiFi helper
    pi/metrics_store.py      SQLite time-series metrics DB (~5 s sample interval)
    pi/log_buffer.py         In-process log ring for /api/logs
    pi/chat_router.py        Regex NL command router (zero-LLM fast path, 14 commands)
    pi/agent.py              RoverAgent: Ollama tool-use agentic loop (35 tools, fast-path, spoken agent)
    pi/agent_knowledge.py    Static ROVER2 hardware/software reference injected into agent context
    pi/agent_solutions.py    Diagnostic fix proposals for suggest_fix tool
    pi/vlm_engine.py         VLMEngine: Hailo Qwen2-VL-2B-Instruct scene description
    pi/voice_engine.py       Piper TTS (binary subprocess) + faster-whisper STT + ROVER personality
    pi/audio_router.py       UDP audio router (asyncio, zero-CPU idle) + /ws/audio WebSocket bridge
    pi/config_public.py      Config masking for GET /api/config (redacts secrets)
    pi/config_runtime.py     Live patch application (apply_config_patch())
    pi/config_store.py       YAML validation, merge, and persistence for tuning endpoint
    pi/firmware_flash.py     OTA AVR firmware flash via avrdude (POST /api/firmware/flash)

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
    ← ROUND_ROBIN scheduler: YOLO + VLM share the chip without mode-switching
    ← NOTE: hailo-ollama (GenAI LLM) and VLM (GenAI VLM) are mutually exclusive
      on HailoRT 5.2.0 — firmware only allows one GenAI session at a time (issue #26)
      hailo-ollama is DISABLED via systemd drop-in override (ExecStart=/bin/true).
      Voice agent backend is now CPU llama3.2:1b. Web chat auto-fallback handles this.

    ── Ollama (system service on Pi) ─────────────────────────────────────────
    llama3.2:1b         Agent LLM (tool-use capable; gemma2:2b does NOT support tools)
    gemma2:2b           Available but NOT used for tool-use
    deepseek-r1:1.5b    Available
```

| Service | Port | Notes |
|---------|------|--------|
| `rover2-api.service` | **8082** | ROVER2 control + follow + AI |
| `rover-camera.service` | **8081** | MJPEG (v1 stack; shared) |
| `ollama.service` | **11434** | Local LLM — llama3.2:1b for agent CPU fallback |
| `rover-api` (v1) | 8080 | Stop when testing ROVER2 serial |
| `hailo-ollama` | **8000** | **DISABLED** — HailoRT 5.2.0 GenAI single-session limit (issue #26); re-enable only if VLM not in use |
| `rover2-restore-wifi.service` | — | Boot: re-apply WiFi from `/boot/firmware/network-config` |
| `hostapd.service` | — | ROVER2 WiFi AP on wlan1 (RTL8812AU), SSID: ROVER2, ch 6 |
| `dnsmasq.service` | — | DHCP + DNS for AP network (10.0.0.10–50, rover.local) |
| `rover2-wlan1-ip.service` | — | Sets static IP 10.0.0.1/24 on wlan1 before hostapd starts |
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
- **ROVER Face PWA** — `/face/` served by rover2-api; voxel face canvas, 8 states, WebSocket telemetry
- **Face canvas status text** — bottom-centre label rendered in iris colour at 70% opacity; 11 px monospace, 3 px letter-spacing, uppercase. Voice states take priority over robot states: LISTENING… / THINKING… / SPEAKING → FOLLOWING / SEARCHING / OBSTACLE DETECTED / ALERT / MOVING / OFFLINE. IDLE = no text (clean face). Updated on every telemetry tick, VAD transition, and TTS start/end
- **Face debug overlay** — top-left corner, `position:fixed;top:0;left:0;z-index:9999`, always visible on load. Rows (in order): MIC, LEVEL, VAD, SESSION, LAST, ROUTED, WSS. LEVEL updates every 150 ms from `_rmsLevel()`. Tap to hide/show. Service-worker cache on `rover-face-v27`
- **Face initial state** — `faceState` initialises as `STATE.IDLE` (not OFFLINE); face shows normally on page load. `_ws.onopen` snaps to IDLE immediately; `_ws.onclose` transitions to OFFLINE and shows "OFFLINE" in status text
- **Voice conversation concurrency fix** — `_convLoopActive` guard at outer `_convLoop` prevents duplicate invocations; `if (!_conversation) return` at start of inner `loop()` stops runaway iterations when the 10-second silence timeout fires `_end()` mid-fetch
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
- **AI Agent** — 35 tools (31 read-only + 4 dangerous); primary backend: hailo-ollama `qwen2.5-instruct:1.5b` on AI HAT+; CPU fallback: `llama3.2:1b`; 10 web-chat fast-path patterns; voice uses 12 spoken fast-path patterns
- **Agentic voice assistant** — `run_spoken_turn()` routes voice through tool-use loop; 12 spoken fast-path patterns; instant canned responses for actions; hailo for data queries; `POST /api/arm/wave` endpoint
- **VLM scene description** — Hailo Qwen2-VL-2B-Instruct, confirmed working on HailoRT 5.2.0; background preload at t+40s, ready ~80s after startup; `/api/vision/describe` returns real scene descriptions
- **Wake word detection** — faster-whisper tiny, `vad_filter=False` on both wake and conversation paths (A32 RMS VAD is the sole gate). Wake word "rover" matched with German-accent fuzzy variants: rower, rofer, roffer, rofar, over, rove, robo, robot, mover, dover, lover. `beam_size=3`, `language="en"` forced on wake path
- **Wake chime** — two-tone ascending chime (880 Hz → 1320 Hz, 70 ms apart, 80 ms each, 5 ms attack / 30 ms release) plays on A32 via Web Audio API on wake confirmation, before conversation starts
- **Full voice pipeline verified** — wake → chime → "Yes, sir." → conversation → TTS reply → social closing ends session → returns to wake listening. Fast-path commands ~0.1 s, hailo-ollama path ~3–4 s
- **AudioSourceManager** — built-in mic fallback when MINIMIC1 not plugged in; `devicechange` event re-enumerates sources on hot-swap. MIC row in debug overlay shows active track label. No polling
- **MegaPi disconnect tolerance** — `body_tracker.set_enabled()` and `POST /api/tracking` no longer raise 500 when MegaPi serial is not connected; `RuntimeError` caught in `try/except`, `WARNING` logged, tracking state updates correctly
- **Face PWA visual overhaul (2026-05-30)** — pixelated block-grid face: narrower oval (W×0.28 × H×0.38), steeper edge dispersal, almond eyes (wide/narrow ellipse, centred ±0.36, −0.20), resting smile (parabolic mouth curve corners-up). SW cache `rover-face-v27`
- **Three-page swipe navigation** — swipe right = help page, swipe left = controls page; percentage-based `translateX` (reliable on Android Chrome); page indicator dots
- **Control panel PAGE 1** — camera feed (MJPEG), follow mode selector (OFF/CAMERA/FUSED/BLE), D-pad, arm lift, gripper. Canvas hidden on PAGE 1 (was obscuring controls via z-index:2 stacking)
- **TTS male voice** — `_makeTtsUtterance()` prefers Daniel (GB male) > any en-GB non-female > en-US male > default; logs selected voice name to console
- **TTS speech bubble (2026-06-01)** — face PWA overlay: right side at ~63% height (mouth level), left-pointing tail, fade-in 150ms / fade-out 400ms, max 140 chars, auto-hide fallback, `pointer-events:none`, page-0 only
- **Guard mode** — BLE-triggered arm/disarm (DISARMED → ARMED when beacon RSSI drops below threshold for 10s), Hailo person detection for intruder alert, Home Assistant webhook integration, TTS announcements; enabled=false by default in config.yaml
- **Camera idle sleep** — when FOLLOW/DETECT both off and ultrasonic variance ≤8 cm for 90s, proxy stops pulling from rover-camera (zero camera bandwidth); wake triggers: ultrasonic delta >8 cm, tracking enable, stream client connect
- **Thermal monitor** — on every `/api/services` call: read vcgencmd, auto-throttle CPU to 1.6 GHz at 80°C, restore to 1.8 GHz when cool; alerts published to WebSocket
- **Home Assistant integration (2026-06-03)** — `pi/ha_client.py`: async HAClient (5s timeout, 13 entity name mappings); voice fast-path patterns for room temperatures and home status (`ha_get_temperatures`, `ha_get_status`); `ha_toggle` for switches/lights/media (dangerous, requires confirm). HA at `http://192.168.225.10:8123`; token in `/etc/rover2.env`. `ha_available` in `/api/status`.
- **ROVER2 WiFi AP (2026-06-03)** — wlan1 RTL8812AU, SSID: ROVER2, 10.0.0.1, 2.4GHz ch 6. Config in `scripts/ap/`. TLS cert covers 10.0.0.1. AP and home WiFi (wlan0) run independently.
- **TLS cert from homelabca (2026-06-03)** — self-signed cert replaced by homelabca-issued cert via step-ca at `192.168.70.14:9000`. Valid 1 year. SANs: `10.0.0.1, 192.168.250.254, 192.168.70.11, 10.62.118.51, rover.local`. Renewal: `./scripts/renew-cert.sh`.
- **Web chat auto-fallback (2026-06-03)** — when hailo-ollama returns 500 or is unreachable, web chat (`run_turn()`) falls back to CPU Ollama (llama3.2:1b) automatically. `_hailo_up` flag tracks health per-call; auto-recovers when HAT reconnects. `/api/chat/status` shows actual active backend.
- **Person detection overlay (2026-06-03)** — `canvas#detection-overlay` on camera feed; JARVIS corner accents; label with confidence % and distance. Clears on camera idle. Requires Hailo HAT for detections.
- **Main web UI redesigned (2026-06-03)** — brutalist monospace (#4a9eda blue, Courier New, sharp corners). STATUS table now includes WEBSOCKET / SERIAL / MOTORS / FIRMWARE rows at top.
- **Speech bubble scrolling (2026-06-03)** — face PWA TTS bubble: max-height 40vh, auto-scroll animation for long replies (60px/s), static when text fits, full text (no truncation).
- **Network-agnostic face PWA (2026-06-03)** — `PI_HOST = window.location.hostname`; camera via HTTPS proxy (`:8082/stream`); works on home WiFi, AP, Tailscale without code changes.

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
                 → POST /api/voice/converse → run_spoken_turn() agentic loop:
                   1. regex pattern → direct tool call + canned/hailo reply (~0.1–4 s)
                   2. hailo-ollama conversational fallback (~3 s)
                   3. CPU llama3.2:1b without tools (~10 s)
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

- **Primary model:** `qwen2.5-instruct:1.5b` via hailo-ollama on AI HAT+ (port 8000, `agent.backend=hailo`)
- **CPU fallback model:** `llama3.2:1b` via Ollama (port 11434; used when hailo-ollama unavailable)
- **Tools:** 35 total — 31 read-only + 4 dangerous (require Confirm button)
- **Web-chat fast-path:** 10 patterns skip Ollama, call tool directly, format result in Python
- **Spoken fast-path:** 12 patterns in `_SPOKEN_FAST_PATTERNS` for voice agent

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
- `turn_speed: 210`, `forward_speed: 170`, `confidence: 0.40`, `centre_zone: 0.30`, `frame_interval_s: 1.0`

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

- Auto-picks first reachable host: `rover-eth`, `<ROVER_ETH_IP>`, WiFi, Tailscale.
- When `agent.backend=hailo` (default): runs `setup_ai_on_hailo.sh` to **enable** hailo-ollama. When `backend=cpu`: starts ollama. When `backend=tools_only`: disables both.
- Runs `scripts/link_hailo_for_rover2.sh` on Pi.
- Sideloads `httpx` and `bleak` if pip fails.
- Applies `OLLAMA_KEEP_ALIVE=1m` via `/etc/systemd/system/ollama.service.d/override.conf` (saves 1.5–2.5 GB RAM vs. default 5-min cache).
- Runs `scripts/disable-desktop.sh` (sets `multi-user.target`, disables labwc/wireplumber, saves ~300 MB RAM). Idempotent — no-op once already set.

```bash
ROVER2_SKIP_VIRTUAL_DONGLE=1 ./deploy_pi.sh   # code-only, skip power profile
```

Logs:
```bash
ssh ambassad0r@<ROVER_WIFI_IP> "journalctl -u rover2-api -f"
```

Verify:
```bash
ssh ambassad0r@<ROVER_WIFI_IP> "curl -sk https://localhost:8082/api/status | python3 -m json.tool"
# Expect: tracking_available true, tracking_hailo_ready true (only after DETECT/FOLLOW first enabled),
#         ble_available true, ble_seen true, ble_follow_enabled true
# Note: -k flag needed for curl with self-signed cert
```

---

## Web UI

**https://<ROVER_WIFI_IP>:8082/** (WiFi) or **https://<ROVER_ETH_IP>:8082/** (eth) or **https://10.0.0.1:8082/** (ROVER2 AP)

> **HTTPS only** — rover2-api runs with a self-signed TLS cert. Use `https://`. First visit: accept the cert warning (Advanced → Proceed). The UI auto-selects `wss://` for WebSocket when served over HTTPS.
>
> TLS cert SANs: `IP:10.0.0.1, IP:192.168.250.254, IP:192.168.70.11, IP:10.62.118.51, DNS:rover.local, DNS:rover` — renewed 2026-06-03 (added 10.0.0.1).

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

Endpoint: `wss://<ROVER_WIFI_IP>:8082/ws`

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
  frame_interval_s: 1.0        # 1fps — CPU optimisation (was 0.25 / 4fps)
  hailo_warmup_s: 30.0         # delay before Hailo loads — lets CPU idle after boot
  confidence: 0.40
  centre_zone: 0.30
  target_bbox_width: 0.35
  ble_follow_enabled: false    # BLE fallback off by default — enable via UI BLE button

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

**Pi system state (applied by `deploy_pi.sh`):**
- Default runlevel: `multi-user.target` (desktop disabled — saves ~300 MB RAM). Applied by `scripts/disable-desktop.sh`. To check: `systemctl get-default`
- Ollama model cache: `OLLAMA_KEEP_ALIVE=1m` via `/etc/systemd/system/ollama.service.d/override.conf` (was 5 min default — saves 1.5–2.5 GB when idle). To check: `systemctl show ollama -p Environment`

---

## Pi system files (outside repo)

Files on the Pi that are **not** in the Git repo and must be created manually on a fresh install or added to the setup script.

| File | Purpose |
|------|---------|
| `/opt/rover2/rover.key` + `rover.crt` | Self-signed TLS cert (SANs: 10.0.0.1, 192.168.250.254, 192.168.70.11, 10.62.118.51, rover.local). Regenerate with `openssl req -x509 ...` if lost. |
| `/opt/rover2/voices/en_GB-cori-high.onnx` | Piper TTS voice model — downloaded once, excluded from rsync `--delete`. |
| `/opt/rover2/whisper-models/` | faster-whisper tiny model — downloaded on first transcribe call. |
| `/etc/rover.env` | Read by **rover-camera** (not rover2-api): ROVER_TTS_RATE, ROVER_TTS_PITCH, ROVER_PI_IP, ROVER_CAMERA_FPS=10 |
| `/etc/rover2.env` | Read by **rover2-api** via `EnvironmentFile=-/etc/rover2.env` in systemd unit: HA_URL, HA_TOKEN (see below) |

**`/etc/rover2.env` — rover2-api runtime secrets (create manually on fresh Pi):**
```
HA_URL=http://192.168.225.10:8123
HA_TOKEN=<home-assistant-long-lived-token>
```
Without this: `ha_available: false`, all HA voice commands silently disabled.

**`/etc/rover.env` — rover-camera runtime config (NOT read by rover2-api):**
```
ROVER_TTS_RATE=0.85
ROVER_TTS_PITCH=0.9
ROVER_PI_IP=192.168.250.254
ROVER_CAMERA_FPS=10
```
Must be created manually on a fresh Pi setup.

---

## Known issues / notes

25. **body_tracker CPU optimisation applied (2026-06-05)** — three changes reduced detect-only CPU from 90–110% to ~16–22%:
   - **TurboJPEG 1/2-scale decode**: `PyTurboJPEG>=1.8,<2.0` installed in venv (requires libjpeg-turbo 2.x on Pi; 2.0 requires libjpeg-turbo 3.x). Decodes 640×480 JPEG directly to 320×240 RGB in one step — eliminates `cv2.imdecode`, `np.frombuffer`, and `cvtColor`. ∼4× faster per decode.
   - **frame_interval_s: 1.0** (was 0.25/4fps → 0.5/2fps → now 1.0/1fps): fewer Hailo inference calls per second.
   - **Stream-skip sleep**: the MJPEG camera generator (camera.py) has no rate limiting — body_tracker was spin-reading and discarding frames at full socket speed (~80% CPU just from the read loop). Fixed by sleeping for `(remaining_interval − 100 ms)` in the skip path and resetting the buffer, limiting tight-loop reading to the final 100 ms before each decode.
   - **Idle CPU (tracking OFF)**: ~0–2% ✓
   - **Detect-only CPU (1fps, TurboJPEG 320×240)**: ~16–22% ✓ (measured 2026-06-05 on Pi 5 @ 1800 MHz)
   - Note: `scaling_factor=(1, 2)` assumes 640×480 camera source; if camera resolution ever changes, verify the scaling produces a sensible decoded size.

1. **USB cable 15W ceiling** — Viking bank + standard USB-C cable = 5V/3A = 15W. Pi+Hailo needs ~18–20W. Cutoff after ~46s inference. **Fix: 5A/100W e-marked cable ordered.** Until then, use mains.
2. **cmdline.txt corruption on hard power cut** — FAT32 boot partition, no journaling. Fixed by mounting boot partition read-only. If it happens again, connect SSD to laptop and restore cmdline.txt manually (PARTUUID=a6f9ddfb-02).
3. **NM state corruption on hard power cut** — WiFi won't connect. Fix: connect SSD, `rm /var/lib/NetworkManager/NetworkManager.state timestamps seen-bssids`, reboot.
4. **BLE false positives** — Samsung UUID `fcf1` is common. In a dense apartment many devices advertise it. Disable BLE (UI button) if false positives cause unwanted motion during camera testing.
5. **Hailo first-enable delay** — Hailo loads on first DETECT/FOLLOW enable with 30s post-boot warmup. First enable after boot may take a few seconds before inference starts.
6. **llama3.2:1b for agent** — gemma2:2b does NOT support tool-use in Ollama. Do not switch.
7. **hailo-ollama** — currently **DISABLED** (see issue #26). The HailoRT 5.2.0 firmware only supports one GenAI session at a time; hailo-ollama (LLM port 12145) and VLM (port 12147) are mutually exclusive. `deploy_pi.sh` setup_ai_on_hailo.sh script still exists but must NOT be run until issue #26 is resolved. Voice agent uses CPU llama3.2:1b fallback.
8. **Pi TCP/443 blocked at gateway** — pip fails for external packages. deploy_pi.sh sideloads wheels.
9. **BLE MAC randomisation** — Android 10+ randomises BLE MAC every ~15 min. Always match by UUID.
10. **Browser cache** — Hard-refresh (`Ctrl+Shift+R`) after every deploy.
11. **piper-phonemize has no cp313 wheel** — Python piper-tts API unusable on Pi's Python 3.13. Solution: use `piper` binary at `/opt/rover2/piper/piper` (statically linked, installed by `deploy_pi.sh`). voice_engine.py calls it as a subprocess.
12. **voices/ excluded from rsync** — `deploy_pi.sh` uses `--exclude 'voices/'` to protect downloaded models from `--delete`. Voice model lives at `/opt/rover2/voices/` on Pi only.
13. **STT backend: faster-whisper** — `faster-whisper` tiny (int8, CPU, ctranslate2, ~150 MB RSS on first transcribe call) is the STT backend. `openai-whisper`/`torch` are NOT installed or used. RSS base is ~92 MB; after first transcribe call it rises to ~240 MB and stabilises. `webkitSpeechRecognition` removed (requires Google servers). Wake/conversation audio captured on A32 via `MediaRecorder` + `AnalyserNode` RMS VAD and POSTed to Pi.
14. **Whisper download cache** — faster-whisper downloads `tiny` model on first call to `/opt/rover2/whisper-models/`. This directory is excluded from rsync `--delete` to protect the downloaded model.
15. **Face PWA on A32** — server now runs HTTPS, so getUserMedia works without `chrome://flags`. Accept the self-signed cert warning once on first visit to `https://<ROVER_WIFI_IP>:8082/`.
16. **HTTPS self-signed cert** — Browsers warn on first visit. Accept once (Advanced → Proceed). Curl on Pi needs `-k` flag. Cert files: `/opt/rover2/rover.key` + `rover.crt` (owned root:ambassad0r, mode 640). Not in repo — regenerate with `openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -keyout rover.key -out rover.crt -days 3650 -nodes -subj "/CN=rover"` if lost.
17. **Left/right direction was inverted** — Fixed 2026-05-26 by swapping direction_map in config.yaml: `left: [1,0]`, `right: [0,-1]`. Root cause: PORT1B is wired to the physical right motor, so firmware's "left motor" is actually the right wheel. D-pad, follow, BLE turns, and obstacle avoidance all fixed by this single config change.
18. **MegaPi on AA batteries** — Brand new AAs may not provide enough current for arc turns at speed 210. If motors stall, use MegaPi mains or reduce turn_speed.
19. **MINIMIC1 speaker muting** — Veles-X MINIMIC1 TRRS lavalier mic plugged into A32 causes Android to mute the main speaker (routes to non-existent earpiece). `setSinkId('speaker')` and silent-buffer AudioContext workarounds are ineffective on Android Chrome. Hardware fix (insulating tape / nail polish on the Ring 2 / microphone contact of the TRRS plug to make Android see it as a 3-pole jack) not yet applied. **Workaround: use built-in mic (unplug MINIMIC1).**
20. ~~**VLM blocked — HailoRT 5.2.0 upgrade required**~~ — **resolved 2026-06-05**: HailoRT upgraded to 5.2.0 (source build from `hailo-ai/hailort` v5.2.0). Full stack: `libhailort.so.5.2.0`, `hailo_platform` Python bindings, PCIe driver rebuilt from `hailo-ai/hailort-drivers` v5.2.0, Hailo-10H firmware 5.2.0 flashed. VLM (`Qwen2-VL-2B-Instruct.hef`) now loads and returns scene descriptions. Body tracker unaffected — confirmed `tracking_hailo_ready: true`. See issue #26 for the hailo-ollama/VLM mutual exclusion constraint discovered during upgrade.
21. ~~Hailo HAT disconnected~~ — **resolved 2026-06-05**: HAT was always physically connected; driver was simply not built for the new kernel (`6.18.33`). Fixed by DKMS (see issue #24). `tracking_available: true`, agent backend `hailo` with `qwen2.5-instruct:1.5b` confirmed working.
24. **Hailo PCIe driver not loaded after kernel upgrade (2026-06-05)** — after `apt upgrade` upgraded kernel to `6.18.33+rpt-rpi-2712`, the `hailo1x_pci` module was missing for the new kernel. Driver source at `/usr/src/hailort-pcie-driver/linux/pcie/` has `dkms.conf.in` + `Makefile`. Fix applied: `cd /usr/src/hailort-pcie-driver/linux/pcie && sudo make install_dkms` — copies source to `/usr/src/hailo1x_pci-5.1.1/`, generates `dkms.conf` (with `AUTOINSTALL=yes`), builds and installs module. DKMS now registered: `dkms status` shows `hailo1x_pci/5.1.1, 6.18.33+rpt-rpi-2712: installed`. **Prevention:** `AUTOINSTALL=yes` in `dkms.conf` means DKMS rebuilds automatically on future kernel upgrades. Alternatively pin the kernel: `sudo apt-mark hold raspberrypi-kernel`. **Recovery:** if driver is missing after an upgrade, run `sudo dkms autoinstall` or repeat the `make install_dkms` step above.
22. **Wake word unreliable on built-in A32 mic** — low RMS (~1.7%) causes missed wake events. Revisit with MINIMIC1 hardware fix (Ring 2 tape) or mic gain adjustment in face/index.html (`WAKE_RMS_THRESHOLD`).
23. **Web chat slow without Hailo** — llama3.2:1b on CPU Ollama takes ~20-30s per reply. Auto-recovers to hailo-ollama (~3s) when HAT reconnects.
26. **hailo-ollama and VLM are mutually exclusive on HailoRT 5.2.0** — The Hailo-10H firmware (5.2.0) only allows ONE GenAI session at a time: either the LLM session (port 12145, used by hailo-ollama) OR the VLM session (port 12147, used by VLM engine). When hailo-ollama is running, VLM gets `HAILO_COMMUNICATION_CLOSED(62)`. **Current decision:** hailo-ollama disabled (systemd override at `/etc/systemd/system/hailo-ollama.service.d/override.conf` sets `ExecStart=/bin/true`, `Restart=no`); VLM owns the Hailo chip for GenAI; voice agent falls back to CPU `llama3.2:1b` (~20-30s). **To restore hailo-ollama:** delete the override file and edit `rover2-api.service` to add back `Wants=hailo-ollama.service`. Resolution requires either: (a) Hailo firmware update that lifts the single-session GenAI limit, or (b) time-multiplexed session management in rover2-api (open/close GenAI session per request rather than holding it open permanently). Body tracker (VDMA inference, NOT GenAI) is unaffected by either session.

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
| U0 — Untethered (battery) | **PASS** — Viking bank + 5A cable, ~16–22% CPU detect-only (2026-06-05) |
| M1 — Follow mobility | **PASS** — untethered WiFi + battery, full follow sequence (2026-06-05) |

```bash
./scripts/run_local_tests.sh              # dev PC — unit tests
./scripts/check_rover_ready.sh            # Pi preflight
```

---

## Next session — recommended work

> **Standing goal:** reduce Pi resource usage as much as possible before and after every feature.

**Completed (2026-06-05):** ✓
- U0: Pi + Hailo sustained on Viking bank with 5A e-marked cable — PASS
- M1: untethered WiFi + battery follow — PASS
- v1.0.0 tagged and merged to main

**Remaining formal tests (mains, WiFi):**
- Complete T1.3–T1.7 formal sign-off (follow, turn, advance, obstacle steer)
- Complete T2 (D-pad, stop, gripper, arm, ultrasonic, forward block)
- Complete T3 (BLE beacon seen, handoff, reacquire)
- Log all results in TEST_RESULTS.md

**Voice pipeline (fully local, agentic — 2026-05-29/30):** ✓ done and verified end-to-end
- Say "rover" → A32 RMS VAD → 2.5 s MediaRecorder chunk → `POST /api/voice/wake` → faster-whisper (vad_filter=False) → fuzzy match → wake chime → "Yes, sir." → conversation loop → `run_spoken_turn()` → hailo/tool/canned reply → Web Speech TTS → social closing → back to wake listening
- **Natural commands:** hello (wave arm), how are you (live diagnostics), follow me, stop following, what do you see, run diagnostics, fix it
- **Timing:** action commands ~0.1 s, hailo-ollama path ~3–4 s
- To test offline: enable airplane mode on A32, visit `https://<ROVER_WIFI_IP>:8082/face/`, say "rover"
- For office demo with no BLE: set `ble_tracker.enabled: false` in config.yaml → deploy
- **Tip:** use built-in A32 mic (MINIMIC1 mutes speaker — see known issue #19)

**Face PWA (2026-05-30):** ✓ done
- Canvas status text (bottom-centre, iris colour) and debug overlay (top-left, 7 rows) both deployed
- Face no longer shows dark/offline on page load — starts IDLE
- Voice conversation no longer fires duplicate LLM requests
- AudioSourceManager: built-in mic fallback, devicechange hot-swap, MIC row shows active track label
- Visual overhaul: almond eyes, resting smile, narrower oval, steeper dispersal, swipe pager, controls on PAGE 1
- Service worker on `rover-face-v27`; unregister SW on A32 after any deploy

**Face PWA speech bubble (2026-06-01):** ✓ done
- TTS speech bubble on right side at ~63% height, left-pointing tail, 150ms fade-in / 400ms fade-out
- Hooks into `_ttsSpeak()` and `_ttsSpeakAsync()` — shows at speak start, hides at onend/onerror
- Service worker on `rover-face-v27`

**Backlog:** `docs/BACKLOG.md` — confirmed desirable work not yet scheduled (web GUI audit, etc.)

**Next session priorities:**
- **Investigate GenAI session multiplexing to re-enable hailo-ollama alongside VLM (issue #26)** — options: (a) time-multiplexed session open/close per request; (b) shared asyncio lock queuing requests; (c) wait for Hailo firmware lifting the single-session limit; (d) verify ROUND_ROBIN doesn't apply to GenAI sessions
- T1.4–T1.7 follow tests (advance, hold, obstacle, BLE fallback)
- HA voice: room clarification when query is ambiguous; more natural response style
- Test `ha_toggle` via voice: "turn on the office light"
- MINIMIC1 hardware fix (Ring 2 tape) — restore lavalier mic, fix speaker muting
- Certificate auto-renewal via cron on central-computer
- Wake word: tune `WAKE_RMS_THRESHOLD` if still unreliable after mic fix

**Software options:**
- "ME only" follow: BLE + camera must agree before following (prevents false positives in dense BLE environments)
- Activity timeline: log follow events to SQLite; show in TOOLS
- Tune `WAKE_RMS_THRESHOLD` (0.015) in face/index.html if false wakes or misses in noisy environments
- **MINIMIC1 hardware fix** — insulate Ring 2 contact on TRRS plug (tape/nail polish) so Android sees it as a 3-pole jack and keeps speaker active. Low risk; no software change needed

**Tuning knobs:**
```yaml
body_tracker:
  turn_speed: 210        # lower if turns overshoot
  forward_speed: 170     # lower if advance too fast
  target_bbox_width: 0.35  # higher (0.45) to stop farther away
  centre_zone: 0.30      # higher (0.40) for looser centering
  confidence: 0.40       # lower (0.30) if person not detected reliably
  hailo_warmup_s: 30.0   # reduce after 5A cable — less need to wait
  frame_interval_s: 1.0  # 1fps; raise to 0.5 (2fps) if follow feels sluggish (costs ~+15% CPU)
```

### Suggested Claude Code prompt

```
Read HANDOFF.md in full before making changes.

Continue ROVER2 development. Pi at <ROVER_WIFI_IP> (WiFi) or <ROVER_ETH_IP> (eth).
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

- After Pi code changes: `./deploy_pi.sh` (targets **<ROVER_ETH_IP>** by default) or rsync manually
- After firmware changes: `./scripts/flash_firmware.sh`
- Do **not** set global `PYTHONPATH` to system site-packages on rover2-api
- Port **8082** for ROVER2; do not change v1 **8080** without coordination
- Only one of `rover-api` / `rover2-api` may use `/dev/ttyUSB0`
- `hailo-ollama` is currently **DISABLED** (see issue #26) — VLM and hailo-ollama cannot coexist on HailoRT 5.2.0 firmware; voice agent uses CPU `llama3.2:1b` fallback instead
- Agent CPU fallback model must be `llama3.2:1b` — gemma2:2b does not support tool calls in Ollama
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
| **Memory** | rover2-api base RSS target: <150 MB (measured ~92 MB at idle). Watchdog thresholds: warn 280 MB, target 300 MB, critical 400 MB (whisper loads ~150 MB RSS on first transcribe). |
