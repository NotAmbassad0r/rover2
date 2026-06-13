# HANDOFF.md — ROVER2

Last updated: 2026-06-13 (heartbeat 10→60s for demo; camera idle suppression logging; HEARTBEAT STATUS row)

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
- rover2-api memory target: ~95 MB RSS idle (whisper unloaded). ~350–420 MB post-voice (whisper loaded, auto-unloads 5 min). Watchdog thresholds whisper-aware.

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
| wlan1 (AP) | **10.0.0.1** | ROVER2 AP — on when away from home; auto-off on home WiFi |
| Tailscale | <ROVER_TAILSCALE_IP> | Remote access |

**Dev machine has no route to <ROVER_WIFI_IP>** — deploy always via eth0 (rover-eth). Use WiFi IP only from a device on the same WiFi (phone, laptop on local network).

**ROVER2 WiFi AP (wlan1 — RTL8812AU dongle):**
- SSID: `ROVER2` · Password: `6nat0n6cNZQ5Q5` · Band: 2.4 GHz ch 6
- Pi IP on AP network: `10.0.0.1` · DHCP range: `10.0.0.10–10.0.0.50`
- Web UI from AP: **`https://10.0.0.1:8082/`**
- DNS: `rover.local` → `10.0.0.1` (via dnsmasq on wlan1)
- Services: `hostapd` + `dnsmasq` + `systemd-networkd` (`10-rover2-ap.network` assigns 10.0.0.1/24 with `ConfigureWithoutCarrier=yes`)
- **Auto-toggle**: NM dispatcher (`scripts/ap/99-rover2-ap`) stops AP when wlan0 connects to home WiFi; starts AP when wlan0 disconnects. Saves ~0.5–1W on battery. Boot-time state synced by `rover2-ap-boot.service` (runs `scripts/ap/rover2-ap-sync.sh` 5s after NM ready).
- Driver: `rtw88_8812au` (in-kernel, no DKMS — survives kernel upgrades automatically)
- wlan0 and wlan1 are independent — AP does not affect home WiFi connection

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
    pi/hailo_session.py      Shared asyncio.Lock for Hailo GenAI sessions (LLM + VLM serialised; HailoRT 5.2.0 single-session limit)
    pi/vlm_engine.py         VLMEngine: Hailo Qwen2-VL-2B-Instruct scene description (per-request sessions)
    pi/voice_engine.py       Piper TTS (binary subprocess) + faster-whisper STT + ROVER personality
    pi/audio_router.py       UDP audio router (asyncio, zero-CPU idle) + /ws/audio WebSocket bridge
    pi/config_public.py      Config masking for GET /api/config (redacts secrets)
    pi/config_runtime.py     Live patch application (apply_config_patch())
    pi/config_store.py       YAML validation, merge, and persistence for tuning endpoint
    pi/firmware_flash.py     OTA AVR firmware flash via avrdude (POST /api/firmware/flash)
    pi/watchdog.py           Autonomous health monitor: services, network, Hailo, serial, memory,
                             CPU, thermal, disk, TLS cert, boot partition, env files; auto-remediates
                             where safe (30s cycle, per-check retry limit, wlan0-aware AP logic)

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
    ← issue #26 RESOLVED: hailo_session.py asyncio.Lock serialises all GenAI opens.
      hailo-ollama binary stays DISABLED (ExecStart=/bin/true override). Instead,
      rover2-api opens hailo_platform.genai.LLM directly per-request, acquires the
      shared lock, generates, calls release() — same lock that VLM uses. No session
      is held between requests. Idle CPU unchanged (no polling, no spin loop).

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
| `hailo-ollama` | **8000** | **DISABLED** — rover2-api uses `hailo_platform.genai.LLM` directly; hailo-ollama binary stays disabled (override: ExecStart=/bin/true). Do not re-enable. |
| `rover2-restore-wifi.service` | — | Boot: re-apply WiFi from `/boot/firmware/network-config` |
| `hostapd.service` | — | ROVER2 WiFi AP on wlan1 (RTL8812AU), SSID: ROVER2, ch 6 |
| `dnsmasq.service` | — | DHCP + DNS for AP network (10.0.0.10–50, rover.local) |
| `systemd-networkd` | — | Assigns 10.0.0.1/24 to wlan1 via `/etc/systemd/network/10-rover2-ap.network` (`ConfigureWithoutCarrier=yes`) |
| `rover2-ap-boot.service` | — | Boot-time AP state sync: stops AP if wlan0 connected, starts if not |
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
- **TTS** — piper binary (`/opt/rover2/piper/piper`) + **en_GB-cori-high** voice; `POST /api/voice/speak` streams WAV; `length_scale: 0.92` (config key `tts.length_scale`); for proactive server events only. Agent replies spoken via **Web Speech API** on A32 (`speechSynthesis`, British voice, rate 0.95; config key `tts.web_speech_rate`)
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
- **AI Agent** — 35 tools (31 read-only + 4 dangerous); primary backend: `hailo_platform.genai.LLM` (direct, `qwen2.5-instruct:1.5b` HEF) on AI HAT+; CPU fallback: `llama3.2:1b`; 10 web-chat fast-path patterns; voice uses 16 spoken fast-path patterns
- **Agentic voice assistant** — `run_spoken_turn()` routes voice through tool-use loop; 12 spoken fast-path patterns; instant canned responses for actions; hailo for data queries; `POST /api/arm/wave` endpoint
- **Structured output tool-calling via direct Hailo LLM (2026-06-07, issue #26 resolved)** — `_call_hailo_with_tools()` does 2-round `hailo_platform.genai.LLM` call: (1) JSON tool detection, (2) result narration — session held open across both rounds. `_converse_hailo()` and `_hailo_spoken_with_context()` use same direct-Python path. `hailo_session.py` asyncio.Lock serialises all GenAI opens (LLM + VLM). VLM uses per-request sessions. hailo-ollama binary stays disabled (ExecStart=/bin/true override). Config: `agent.hailo_tool_calling: true`. Stats: `GET /api/agent/stats`. DIAG tab AGENT BACKEND panel. `<|im_end|>` EOS tokens stripped from all LLM output. Verified 2026-06-07: session acquire → reply → release logged cleanly.
- **CPU fallback context reduction (2026-06-07)** — `_CPU_TOOLS`: 14-tool subset replacing 38-tool context; warm latency ~20–25s (was ~120–150s). `_SPOKEN_TOOLS`/`_TOOL_NAMES`/`_SPOKEN_TOOL_NAMES` dead code removed.
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
- **Autonomous watchdog (2026-06-05)** — `pi/watchdog.py` expanded to full health monitor; 30s interval; 15 new check domains: services (hostapd/dnsmasq/ssh/tailscaled), network (eth0/wlan0/wlan1-AP), Hailo PCIe module, camera frame stall, MegaPi serial, robot stuck, person lost, stale safety block, memory (stop-ollama + rover2-api restart), CPU sustained high, thermal (follow-disable at 85°C, TTS at 90°C), disk (GB-based with journalctl vacuum + metrics-disable), TLS cert expiry, boot-partition rw guard, env/binary file presence. Per-check retry limit: 3 attempts / 10 min then persistent WS alert. Alerts broadcast to WebSocket `alerts` field. Auto-handled: rover2-api overheating, disk low, stale safety block, Hailo driver unloaded, MegaPi serial drop, robot stuck.
- **MegaPi power monitoring (2026-06-13)** — indirect detection via drive+ultrasonic correlation. `megapi.py` tracks last linear drive command timestamp and ultrasonic snapshot at command time; evaluates delta on next ultrasonic reading (in reader thread, <1ms no serial I/O in watchdog). States: `ok` (delta≥5cm), `low_battery` (delta 2–4cm, ≥3 consecutive), `no_response` (delta<2cm, ≥3 consecutive), `unknown` (no recent drive commands or ultrasonic out of range), `serial_error` (USB disconnected). Watchdog `_check_megapi_power()` reads state and emits alert; skips during follow mode and safety-blocked. Serial write health: `_last_write_ts` tracked in `send()`; reconnect triggered if no write in >60s while connected. WebSocket telemetry: `megapi_power_state`. STATUS table: MEGAPI POWER row (green/amber/red). Face overlay: MOTOR row (green/amber/red). Known limitation: detection requires an obstacle in ultrasonic range — open space reads may stay "unknown".
- **Follow auto-disable fixes (2026-06-13)** — heartbeat_timeout_s increased 10→60s in config.yaml (comment explains demo rationale); ws_control.py watchdog_loop log updated to `[ws] heartbeat timeout after Xs — disabling follow and stopping motors` (includes actual elapsed time); `last_heartbeat_s` and `heartbeat_timeout_s` added to WS telemetry per-client in `_telemetry_loop()`; STATUS table: HEARTBEAT row added (OK/WARNING/TIMEOUT with colour, after UPTIME). camera_idle.py `set_tracking_active()` now logs on state change only (`[camera_idle] tracking active — idle suppressed` / `tracking inactive — idle resumed`). body_tracker.py logs `[body_tracker] camera idle suppressed/released` before each `cam_idle.set_tracking_active()` call in set_enabled/set_detect_only/set_follow_mode. Heartbeat timeout confirmed: only applies to enabled=True follow + motors; detect_only mode is NOT stopped by heartbeat timeout (watchdog active check excludes detect_only).
- **Camera idle / follow conflict fix (2026-06-13)** — camera_idle.py: added `set_tracking_active(enabled: bool)`; when True: camera never sleeps (wake triggered immediately); `notify_tracking_active()` delegates to it. `run()` loop checks `self._tracking_active` first — immediate suppression without waiting for next poll. body_tracker.py: added `set_cam_idle()` setter (cam_idle is created after body_tracker, so wired via setter from `create_app()` in server.py after `CameraIdleManager` is instantiated); `set_enabled()`, `set_detect_only()`, `set_follow_mode()` all call `cam_idle.set_tracking_active(active)` after releasing lock. `_stream_loop()`: first 2 JPEG frames after camera reconnect discarded (`_warmup_frames = 2`) to avoid dark transition frames reaching Hailo. Fixes camera-only follow mode losing person immediately after ultrasonic-triggered wake.
- **Presentation-critical fixes (2026-06-13)** — HA timeout reduced 5→3s in all 4 ha_client.py methods (`get_states`, `get_state`, `call_service`, `ping`); log level changed from WARNING to INFO with consistent `[ha] HA unreachable — office/offline mode` message. DIAG tab HOME ASSISTANT panel shows "HA OFFLINE" (grey) instead of "DISCONNECTED" (red) when unreachable — cleaner for office demo. MegaPi battery estimate: `_motor_performance_samples deque(maxlen=10)` tracks `ratio = observed_delta / expected_delta` (expected = `commanded_speed / 255 * speed_to_cm_factor`); properties `battery_estimate` (good/low/critical/no_response/unknown), `performance_ratio`, `megapi_samples_count`; config `megapi.speed_to_cm_factor: 8.0`; included in WS telemetry and `/api/status`; DIAG tab ROBOT·LIVE shows MEGAPI PORT/POWER/BATTERY/PERF rows. Priority 3 confirmed: WebSocket URL uses `wsUrl()` → `location.protocol + '//' + location.host + '/ws'` (fully dynamic); all 60+ REST calls use `apiBase()` (no hardcoded IPs).
- **Home Assistant integration (2026-06-03)** — `pi/ha_client.py`: async HAClient (5s timeout, 13 entity name mappings); voice fast-path patterns for room temperatures and home status (`ha_get_temperatures`, `ha_get_status`); `ha_toggle` for switches/lights/media (dangerous, requires confirm). HA at `http://192.168.225.10:8123`; token in `/etc/rover2.env`. `ha_available` in `/api/status`.
- **ROVER2 WiFi AP (2026-06-03)** — wlan1 RTL8812AU, SSID: ROVER2, 10.0.0.1, 2.4GHz ch 6. Config in `scripts/ap/`. TLS cert covers 10.0.0.1. AP and home WiFi (wlan0) run independently.
- **AP auto-toggle (2026-06-05)** — NM dispatcher (`/etc/NetworkManager/dispatcher.d/99-rover2-ap`) stops hostapd+dnsmasq when wlan0 connects to home WiFi, starts when wlan0 disconnects. `rover2-ap-boot.service` syncs state at boot (5s delay after NM). Saves ~0.5–1W on battery. Watchdog updated to recognise wlan0-connected as intentional AP-off state (no false restart). Verified with NM simulation: AP up on disconnect, AP off on reconnect, logger confirms both transitions.
- **TLS cert from homelabca (2026-06-03)** — self-signed cert replaced by homelabca-issued cert via step-ca at `192.168.70.14:9000`. Valid 1 year. SANs: `10.0.0.1, 192.168.250.254, 192.168.70.11, 10.62.118.51, rover.local`. Certs: `certs/` dir (public cert + CA root tracked; `rover.key` gitignored). Renewal: `./scripts/renew-cert.sh` (manual) or automatic via monthly cron on central-computer (`scripts/cert-renew-cron.sh`; logs to `~/.rover2/cert-renew.log`). Current cert expires: 2027-06-03.
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
                   1. regex pattern → direct tool call + canned/hailo reply (~0.1 s canned; ~10-15s hailo)
                   2. _call_hailo_with_tools → direct hailo_platform.genai.LLM (~10-15s total)
                   3. _converse_hailo → direct hailo LLM conversational (~5-10s)
                   4. CPU llama3.2:1b without tools (~10 s fallback)
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

- **Primary model:** `qwen2.5-instruct:1.5b` via `hailo_platform.genai.LLM` direct Python API on AI HAT+ (`agent.backend=hailo`; hailo-ollama binary disabled)
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

Telemetry fields include: `tracking_enabled`, `tracking_detect_only`, `tracking_hailo_ready`, `person_detected`, `ble_active`, `ble_follow_enabled`, `ble_available`, `ble_seen`, `ble_rssi`, `alerts`, `ap_state`, `watchdog_last_action`, `watchdog_last_action_ts`, `watchdog_last_cycle`, `voice_pipeline`, `voice_session_active`, `whisper_loaded`, `vad_active`, `tts_active`, `megapi_power_state`.

New REST endpoints (2026-06-05):
- `GET /api/watchdog/status` — detailed watchdog state (last_cycle, last_action, last_action_ts, persistent_alerts, actions_used, **action_history**)
- `GET /api/network/status` — AP state (wlan0_connected, wlan1_ap_active, wlan1_channel, rtw88_8812au_loaded)
- `POST /api/watchdog/test-alert` — inject a test alert into the next telemetry push (dev/debug only)

New REST endpoints (2026-06-07):
- `GET /api/agent/stats` — agent backend stats: last_backend, last_tool, last_tool_ts, hailo_tool_success_count, hailo_tool_fallback_count, hailo_tool_success_rate, hailo_tool_calling_enabled
- `GET /api/alerts/history` — last 50 fired alerts ring buffer
- `DELETE /api/alerts/history` — clear alert history
- `GET /api/diagnostics/journal?service=&lines=&filter=` — journal log viewer with filter param (added `filter` query param to existing endpoint)
- `GET /api/system/resources` — per-process RSS+CPU, Pi temp, disk (already existed)
- `GET /api/follow/status` — live follow pipeline telemetry (already existed)
- `GET /api/voice/status` — now includes `vad_active`, `tts_active` fields

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
  heartbeat_timeout_s: 60.0   # 60s tolerates phone screen dim + tab switches (was 10s)

body_tracker:
  turn_speed: 210              # raised for hard floor (was 100)
  forward_speed: 170           # raised (was 120)
  frame_interval_s: 1.0        # 1fps — CPU optimisation (was 0.25 / 4fps)
  hailo_warmup_s: 30.0         # delay before Hailo loads — lets CPU idle after boot
  confidence: 0.40
  centre_zone: 0.30
  target_bbox_width: 0.35
  ble_follow_enabled: false    # BLE fallback off by default — enable via UI BLE button

vlm:
  cooldown_s: 95.0             # min seconds between HEF reloads — issue #28 (HAILO_SHUTDOWN_EVENT_SIGNALED)

ble_tracker:
  enabled: true        # set false for office demo (saves CPU + heat — no BLE needed)
                       # set true for home use (guard mode arm/disarm via beacon)
  beacon_uuid: "0000fcf1-0000-1000-8000-00805f9b34fb"   # Samsung Z Flip 6
  device_name: "-Lars's Z flip 6"
  device_mac: "F0:05:1B:0A:E0:4C"

watchdog:
  interval_s: 30                 # check interval (30s max)
  ollama_idle_stop_minutes: 2    # stop ollama when CPU < 1% for this long
  cpu_sustained_percent: 90      # threshold for sustained CPU alert
  cpu_sustained_seconds: 60      # seconds above threshold before action
  disk_warn_gb: 2                # free GB — vacuum journal + pip cache
  disk_critical_gb: 0.5          # free GB — disable SQLite metrics writes
  cert_warn_days: 30             # days until TLS cert expiry → alert
  cert_critical_days: 7          # days until expiry → attempt renew
  temp_follow_disable_c: 85      # °C → disable FOLLOW/DETECT via body_tracker
  temp_critical_c: 90            # °C → TTS warning (if not in conversation)
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

29. **ROVER v1 services on Pi (2026-06-07)**
   - `rover-camera.service` — **replaced by `rover2-camera.service` (2026-06-07)**. v1 service stopped, disabled, masked. Port 8081 now served by `pi/camera_server.py` (TurboJPEG quality=85, idle 1fps when 0 clients, JSON `/health`). Watchdog updated to restart `rover2-camera`.
   - `rover-ble.service` (`/opt/rover/hardware/ble_scanner.py`) — **disabled + masked 2026-06-07**. Was 0.7% idle CPU. ROVER2 uses its own in-process bleak scanner (`ble_tracker.py`); v1 scanner was unused.
   - `rover-api.service` — unit file exists, service not running. Leave as-is.

1. **USB cable 15W ceiling** — Viking bank + standard USB-C cable = 5V/3A = 15W. Pi+Hailo needs ~18–20W. Cutoff after ~46s inference. **Fix: 5A/100W e-marked cable ordered.** Until then, use mains.
2. **cmdline.txt corruption on hard power cut** — FAT32 boot partition, no journaling. Fixed by mounting boot partition read-only. If it happens again, connect SSD to laptop and restore cmdline.txt manually (PARTUUID=a6f9ddfb-02).
3. **NM state corruption on hard power cut** — WiFi won't connect. Fix: connect SSD, `rm /var/lib/NetworkManager/NetworkManager.state timestamps seen-bssids`, reboot.
4. **BLE false positives** — Samsung UUID `fcf1` is common. In a dense apartment many devices advertise it. Disable BLE (UI button) if false positives cause unwanted motion during camera testing.
5. **Hailo first-enable delay** — Hailo loads on first DETECT/FOLLOW enable with 30s post-boot warmup. First enable after boot may take a few seconds before inference starts.
6. **llama3.2:1b for agent** — gemma2:2b does NOT support tool-use in Ollama. Do not switch.
7. **hailo-ollama** — **permanently DISABLED** (issue #26 resolved 2026-06-07). rover2-api uses `hailo_platform.genai.LLM` directly instead. `deploy_pi.sh` `setup_ai_on_hailo.sh` script still exists but must NOT be run — hailo-ollama binary is overridden to `/bin/true`. CPU llama3.2:1b is fallback only.
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
27. **RTL8812AU AP carrier loss after kernel upgrade (2026-06-05)** — after kernel `6.18.33+rpt-rpi-2712`, the wlan1 AP interface (RTL8812AU, `rtw88_8812au` in-kernel driver — NOT out-of-tree `88xxau` DKMS) lost carrier at runtime. Root cause: `/etc/systemd/network/10-rover2-ap.network` was missing `ConfigureWithoutCarrier=yes`, so systemd-networkd refused to apply the 10.0.0.1/24 address when carrier was absent. The AP ran but had no IP → dnsmasq `error binding DHCP socket to device wlan1`.
   **Fix applied:** Added `ConfigureWithoutCarrier=yes` and `RequiredForOnline=no` to `10-rover2-ap.network`; added `ctrl_interface=/run/hostapd` to `hostapd.conf` (enables `hostapd_cli` for watchdog checks); restarted stack in order: `systemd-networkd` → `hostapd` → `dnsmasq`.
   **DKMS note:** `rtw88_8812au` is an in-kernel driver (shipped with `6.18.33`). DKMS is NOT installed on this Pi and is NOT needed for the RTL8812AU. Do not run `dkms autoinstall` for this adapter.
   **Watchdog:** `_check_wifi_driver()` monitors `rtw88_8812au` in `lsmod`, attempts `modprobe rtw88_8812au` if missing. `_check_network()` corrected: eth0 check removed (eth0 is built-in GbE with no cable — NO-CARRIER is normal); wlan1 check now verifies both `type AP` in `iw dev` AND `10.0.0.1` in `ip addr`, restarts stack in correct order if either is missing.
   **Recovery:** `sudo systemctl restart systemd-networkd && sleep 2 && sudo systemctl restart hostapd && sleep 2 && sudo systemctl restart dnsmasq`

28. ~~**VLM rapid sequential load crash**~~ — **RESOLVED 2026-06-07** by 95s cooldown in `vlm_engine.describe()`.

   **Root cause:** HailoRT 5.2.0 cannot re-load Qwen2-VL-2B HEF within ~90s of previous release — causes `HAILO_SHUTDOWN_EVENT_SIGNALED(57)` at chunk 14/40, crashing the entire Hailo device state (body_tracker VDMA dies, LLM refuses connections).

   **Fix:** `pi/vlm_engine.py` enforces a 95s minimum gap (`vlm.cooldown_s` config key) between `describe()` calls via `asyncio.sleep()` in `_wait_for_cooldown()`. Timestamp recorded in `_open_and_generate()` `finally` block (covers both success and crash paths). Zero CPU overhead — asyncio.sleep blocks no threads.

   **Files changed:**
   - `pi/vlm_engine.py` — `_cooldown_s` from config; `_last_session_release_ts` in `finally`; `cooldown_remaining()` helper; `_wait_for_cooldown()` async; `get_status()` adds `cooldown_remaining_s` + `last_describe_wait_s`
   - `pi/config.yaml` — `vlm.cooldown_s: 95.0`
   - `pi/server.py` — `/api/agent/stats` includes `vlm_cooldown_remaining_s` + `vlm_last_describe_wait_s`

   **Verified 2026-06-07:** second describe() call logged `vlm: cooldown active — waiting 76.0s before re-loading`; body_tracker ran continuously during wait; second VLM session opened and generated 616 chars cleanly; no device crash; `last_describe_wait_s: 76.0` in response; LLM path unaffected during wait.

26. ~~**hailo-ollama and VLM are mutually exclusive on HailoRT 5.2.0**~~ — **RESOLVED 2026-06-07** via software session multiplexing.

   **Root cause:** HailoRT 5.2.0 only allows one `hailo_platform.genai.{LLM,VLM}` session open at a time per chip. The hailo-ollama binary kept its session open indefinitely, blocking VLM.

   **Resolution:** hailo-ollama binary stays disabled (`ExecStart=/bin/true` override). `pi/hailo_session.py` provides a shared `asyncio.Lock()` (`genai_session(holder)` context manager). All Hailo GenAI opens must hold this lock. LLM is opened, used, and released per-request using `hailo_platform.genai.LLM` directly.

   **Files changed:**
   - `pi/hailo_session.py` (NEW) — shared asyncio lock; `genai_session(holder)` context manager
   - `pi/vlm_engine.py` (REWRITTEN) — per-request sessions; `_open_and_generate()` opens VDevice+VLM, runs inference, calls `vlm.release()`; acquires `genai_session("vlm")` before executor call
   - `pi/agent.py` — `_call_hailo_with_tools()`, `_converse_hailo()`, `_hailo_spoken_with_context()` rewritten to use direct `hailo_platform.genai.LLM` under `genai_session("llm-*")` lock. `_hailo_llm_available()` helper checks HEF file exists. `check_available()` and `_check_availability()` use HEF existence for hailo backend (no HTTP ping). `_run_llm()` helper does open/generate/release in executor.
   - `pi/config.yaml` — `hailo_ollama.enabled: false`, `hailo_ollama.llm_hef_path` set to blob path

   **Session lifecycle (LLM):** `genai_session` lock acquired → executor opens VDevice+LLM (~5s reopen) → `generate_all()` (~2-4s) → `llm.release()` → lock released. For two-round tool-calling (`_call_hailo_with_tools`), LLM stays open across both rounds (tool exec is async during this time) → single `release()` in `finally`.

   **Idle CPU:** zero. `asyncio.Lock()` — no polling, no thread. Lock is only acquired during active LLM/VLM generation.

   **Body tracker (VDMA inference):** unaffected — never uses GenAI session, runs independently.

   **Verification 2026-06-07 (full AI stack):**
   - Step 3 ✓ `hailo_session.py` deployed; `genai_session()` calls present in all LLM/VLM paths; HailoRT 5.2.0 confirmed
   - Step 4 ✓ `_call_hailo_with_tools()` two-round cycle: `llm-tools acquired` → LLM round-1 selected `get_logs {n:10}` → tool executed (55ms) → round-2 narration via `llm.clear_context()` (105 chars) → `llm-tools released`. Total ~17.5s warm. Clean reply, no `<|im_end|>` tokens
   - Step 5 ✓ VLM per-request session: `vlm acquired` → VLMEngine session opened → 389-char scene description → `VLMEngine: session released` → `vlm released`. First-ever VLM load ~86s (HEF loading); subsequent calls faster. `mode: "per-request"` in status
   - Step 6 ⚠ Session lock serializes correctly (VLM held lock, LLM waited and queued). BUT: VLM second load (~76s after first) failed at chunk 14/40 with `HAILO_SHUTDOWN_EVENT_SIGNALED(57)`, crashing entire Hailo device state including body_tracker VDMA. Recovered by `sudo systemctl restart rover2-api`. See issue #28
   - Step 7 ✓ Body tracker resumed after rover2-api restart: `Hailo ready` + `Inference running` + `Detect score: no person boxes`
   - Step 8: RSS 142 MB (target <150 MB ✓); CPU ~10% with detect-only at 1fps (expected, body tracker active)

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

**Backlog:** `docs/BACKLOG.md` — confirmed desirable work not yet scheduled (TTS speed, GenAI session multiplexing)

**Auto-handled by watchdog (2026-06-05):** service restarts (hostapd/dnsmasq/ssh/tailscaled), wlan0 reconnect, wlan1 AP stack restore (checks AP mode + 10.0.0.1 IP, restarts networkd→hostapd→dnsmasq), RTL8812AU driver reload (`rtw88_8812au`), Hailo PCIe module load, camera frame stall, MegaPi serial reconnect, robot stuck detection, stale safety block clear, ollama idle stop, CPU sustained high (ollama), thermal follow-disable (85°C) + TTS (90°C), disk low vacuum, disk critical metrics-disable, TLS cert expiry alert + auto-renew, boot-partition rw guard, env/binary file presence warnings. Note: eth0 is built-in GbE with no cable — NO-CARRIER is normal; watchdog no longer acts on eth0.

**Verified and closed this session (2026-06-05):**
- ✓ Watchdog: all 15 check domains confirmed, disk alert test triggered and cleared, wlan0-aware AP exemption added, 2 watchdog cycles confirmed clean post-fix
- ✓ RTL8812AU: in-kernel `rtw88_8812au` driver confirmed loaded; AP up on channel 6, 10.0.0.1 assigned; no DKMS needed (in-kernel, survives upgrades)
- ✓ AP auto-toggle: NM dispatcher + boot service implemented; NM simulation passed (AP up on disconnect, AP down on reconnect); watchdog no longer fights dispatcher

**Web UI + Face PWA (2026-06-05):** ✓ done
- CONTROL tab STATUS: WATCHDOG row (last action or OK) + AP MODE row (home/away/error)
- DIAG tab WATCHDOG panel: now shows last cycle, last action, action timestamp, alert count
- DIAG tab NETWORK section: WLAN1 AP state + rtw88_8812au driver loaded/not status (from `/api/network/status`)
- Alert bar: last 3 only, CSS severity colour coding (no emoji), correct toast (no TTS)
- New REST endpoints: `GET /api/watchdog/status`, `GET /api/network/status`, `POST /api/watchdog/test-alert`
- WebSocket telemetry extra (5 s tick, not 400 ms): `ap_state`, `watchdog_last_action`, `watchdog_last_action_ts`, `watchdog_last_cycle`
- Face PWA: WDOG + AP rows in debug overlay; SELF-HEALED canvas status (5 s, when watchdog acted <60 s ago and IDLE)
- Face PWA: PAGE 3 info page (ROVER2 overview, capabilities, voice, architecture, live status from WS, built-by)
- Face PWA: 4-page swipe layout (help ← face → ctrl → info); indicator dots updated; sw.js bumped to `rover-face-v35`
- Watchdog: `last_cycle_iso`, `last_action`, `last_action_ts` properties added

**hailo-ollama LLM benchmark (2026-06-06):** ✓ done — decision: **KEEP qwen2.5-instruct:1.5b**

Benchmark method: hailo-ollama started manually (bypassing systemd override); rover2-api stopped to free Hailo GenAI session; both models tested on same hardware with same prompts.

| Metric | qwen2.5-instruct:1.5b | llama3.2:3b | Notes |
|--------|----------------------|-------------|-------|
| Hot TPS | **6.3–6.4 t/s** | 2.5 t/s | Hot = model already loaded |
| Avg TPS (incl. cold) | 4.5 t/s | 1.9 t/s | First run includes ~80s HEF load |
| Hot voice latency | **1.6–3.3 s** (OK) | 18–26 s (TOO SLOW) | 20-token ROVER reply |
| Tool intent (JSON) | 12% | 12% | Neither supports native tool_calls; both poor at JSON format |
| Calls "sir" | 83% | **100%** | llama3.2:3b better persona adherence |
| Short replies | 67% | **83%** | llama3.2:3b more concise |
| Forbidden words | 0 | 0 | Neither said "certainly" / "absolutely" |
| Context injection | weak | **better** | llama3.2:3b uses tool result more naturally |
| qwen2-1.5b-FC-v1 | — | — | Not available in hailo format (HEF not found) |

**Decision: KEEP qwen2.5-instruct:1.5b.** Task criteria: switch if latency <6s + precision ≥ current.
llama3.2:3b hot latency 18–26 s definitively fails the <6 s threshold. Quality marginally better but irrelevant given speed.
llama3.2:3b HEF deleted from hailo-ollama store to free ~3.4 GB. qwen2.5-instruct:1.5b remains.

hailo-ollama note: GenAI session conflict with VLM confirmed. To run a benchmark again: stop rover2-api first, then start hailo-ollama directly (`HAILO_OLLAMA_VDEVICE_GROUP_ID=rover2 HAILO_OLLAMA_GENERATION_TIMEOUT=120 /usr/local/bin/hailo-ollama serve`). Port 8000 (not 12145 — old comment in issue #26 was wrong).

---

**CPU fallback LLM benchmark (2026-06-06):** ✓ done — decision: **KEEP llama3.2:1b**

Benchmark method: stop rover2-api (prevents llama3.2:3b keepalive pings from interfering with ollama scheduler); restart ollama clean; pull candidates; gate test + latency test; stop ollama; restart rover2-api. Verbatim agent.py tool definitions and `_SYSTEM_PROMPT` used (HA-enabled, 38 tools).

**Root cause finding:** the 38-tool + `_SYSTEM_PROMPT` context is ~3000–5000 tokens. All sub-4B models process this in >60 s on Pi 5 (no GPU), making the gate test time out at 120 s. This is a production limitation: `_call_model_cpu` (web chat CPU fallback) may silently time out in production (ollama_timeout_s: 180 s) on the first query.

| Model | Gate (38 tools, 120s) | Pull | Latency notes | Decision |
|-------|----------------------|------|---------------|----------|
| llama3.2:1b (baseline) | FAIL (timeout) | already present | 26.6s cold/3 tools; 60s warm/38-tool-minimal | **KEEP** |
| gemma3:1b | FAIL (HTTP 400 — no tool_calls API) | OK → removed | — | SKIP |
| gemma3:2b | FAIL (pull error) | FAILED | — | SKIP |
| qwen2.5:3b | FAIL (timeout) | OK → removed | 107.9s cold/3 tools; 181s warm/38-tool-minimal → ~400s with full defs | SKIP |

Targeted latency measurement (TIMEOUT=300s, minimal tool defs with short descriptions, model warm):
- `llama3.2:1b`: 3-tool cold = **26.6s**, 38-tool warm = **60.0s** (minimal defs → ~120–150s with full production defs)
- `qwen2.5:3b`: 3-tool cold = **107.9s**, 38-tool warm = **181.2s** (minimal) → exceeds 180s production timeout

**Decision: KEEP llama3.2:1b.** No candidate improves on latency or precision for the 38-tool web chat CPU fallback path. qwen2.5:3b is 3× slower per token and exceeds production timeout even for minimal tool context.

**Secondary finding:** `_SPOKEN_TOOLS` (10-tool subset, pi/agent.py line 484) is **dead code** — the spoken CPU fallback (`_cpu_spoken_no_tools`) sends NO tools at all. Tool_calls are only used by `_call_model_cpu` (web chat fallback, 38 tools).

**Follow-up action needed (not done here):** Reduce `_call_model_cpu` tool context from 38 to ~10–15 essential tools to make CPU fallback practical. Estimated warm latency with 10 tools: ~20–25s (within 180s timeout with headroom). Remove `_SPOKEN_TOOLS` dead code.

Removed from ollama: `gemma3:1b`, `qwen2.5:3b`. Retained: `llama3.2:1b`, `llama3.2:3b`, `deepseek-r1:1.5b`, `gemma2:2b`.

---

**Next session priorities:**
- ✓ **Reduce `_call_model_cpu` tool context** (pi/agent.py) — done 2026-06-07; `_CPU_TOOLS` = 14 tools, warm ~20–25s; dead code removed
- ✓ **Implement structured output tool-calling on hailo-ollama** — done 2026-06-07; `_call_hailo_with_tools()`, config flag, `/api/agent/stats`, DIAG panel
- ✓ **Issue #26 resolved (2026-06-07)** — direct Python LLM via `hailo_platform.genai.LLM`; `hailo_session.py` asyncio lock; VLM per-request; hailo-ollama stays disabled; verified working
- ✓ **Verify VLM concurrency (2026-06-07)** — session lock serializes correctly; LLM waits while VLM holds lock. New issue found: VLM second rapid load crashes device (issue #28)
- ✓ **Test two-round tool-calling (2026-06-07)** — `_call_hailo_with_tools()` confirmed: `llm-tools acquired` → `get_logs {n:10}` parsed → tool executed → round-2 narration → released. ~17.5s warm
- ✓ **VLM cooldown (issue #28 — 2026-06-07)** — 95s cooldown in `vlm_engine.describe()` via `asyncio.sleep()`. `vlm.cooldown_s: 95.0` in config.yaml. `cooldown_remaining_s` + `last_describe_wait_s` in `/api/vision/describe` response and `/api/agent/stats`. Body tracker unaffected during wait. Verified: no crash on rapid second describe.
- ✓ **Web GUI audit (2026-06-07)** — 9 bugs fixed in `pi/web/static/index.html`:
  - Agent stats panel: `diag-label`/`diag-val` CSS classes didn't exist → panel was unstyled text; fixed to `diag-row/label/val`
  - Agent stats panel: VLM cooldown rows now shown when nonzero (API already had the fields)
  - Chat tab VLM detail: shows cooldown remaining when active
  - Detection overlay: label now shows actual `ultrasonic_cm` reading (was always showing safe distance threshold "40 cm")
  - `cameraStreamUrl()`: was hardcoding `'https://'` ignoring computed `proto` variable — fixed
  - Global voice button: initial text was `_ SPEAK TO ROVER` placeholder — fixed to 🎤
  - `wakeCamera()`, `setBleFollow()`, `setFollowMode()`: changed relative `/api/...` URLs to `apiBase() + '/api/...'`
- **LLM session open latency** — first open ~76s (cold + YOLO contention), warm ~17s. No action needed — acceptable for voice use. Consider `_run_llm_warmup()` only if first-query latency complaints arise.
- T1.4–T1.7 follow tests (advance, hold, obstacle, BLE fallback)
- MINIMIC1 hardware fix (Ring 2 tape) — restore lavalier mic, fix speaker muting
- HA voice: room clarification when query is ambiguous; more natural response style
- Test `ha_toggle` via voice: "turn on the office light"
- ✓ Certificate auto-renewal via cron on central-computer — done 2026-06-07 (monthly cron on central-computer: `0 9 1 * *`; `scripts/cert-renew-cron.sh` → `scripts/renew-cert.sh`; tries mTLS renewal first, falls back to admin provisioner; certs in `certs/`; logs to `~/.rover2/cert-renew.log`)

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
- `hailo-ollama` is **permanently DISABLED** — rover2-api uses `hailo_platform.genai.LLM` directly (issue #26 resolved 2026-06-07). Do not re-enable hailo-ollama binary. Spoken voice agent uses direct Hailo LLM path; CPU `llama3.2:1b` is fallback only
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
| **WiFi AP** | Auto-off when on home WiFi (~0.5–1W saving); NM dispatcher + boot service manage state. |
| **Memory** | Base RSS: ~95 MB idle (whisper unloaded). ~350–420 MB during/after voice activity (whisper loaded, auto-unloads after 5 min). Watchdog thresholds whisper-aware: +280 MB added when loaded (warn 560/critical 630/emergency 680 MB). Config: `voice.whisper_unload_after_s: 300`. |
