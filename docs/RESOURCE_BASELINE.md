# RESOURCE_BASELINE.md — ROVER2 Component Measurements

**Date:** 2026-06-07  
**Branch:** dev  
**Pi:** Raspberry Pi 5, Hailo-10H (firmware 5.2.0), 8 GB RAM, SSD  
**Config:** `hailo_warmup_s: 30.0`, `frame_interval_s: 1.0` (1 fps), `heartbeat_timeout_s: 10.0`  
**Power source:** USB-C (mains, 5V/3A) — `EXT5V_V ≈ 4.94–4.97 V`  
**Measurement method:** SSH; `ps aux`, `/proc/PID/smaps_rollup`, `vcgencmd pmic_read_adc`

---

## Clean-state preconditions

- `ollama` stopped (`systemctl stop ollama`)
- `hailo-ollama` permanently disabled (`/bin/true` override)
- `rover2-api` freshly restarted
- Tracking disabled (`POST /api/tracking {"enabled": false}`)
- `hostapd` / `dnsmasq` inactive (home WiFi active on wlan0)

---

## Step 1 — System Idle Baseline

Rover2-api running, no tracking, no inference, no voice.

### CPU

| Sample | time | rover2-api | rover-camera† | ble-scanner† | NetworkMgr | tailscaled | bluetoothd |
|--------|------|-----------|--------------|--------------|------------|-----------|-----------|
| 1 | 19:14:35 | 1.9% | 8.6% | 0.7% | 0.7% | 0.1% | 0.5% |
| 2 | 19:14:45 | 1.9% | 8.6% | 0.7% | 0.7% | 0.1% | 0.5% |
| 3 | 19:14:55 | 1.8% | 8.6% | 0.7% | 0.7% | 0.1% | 0.5% |

† `/opt/rover/` (old rover project) still running — 8.6% CPU constant, not rover2.

### CPU Governor / Frequency

- Governor: `schedutil` ✓
- Frequency at idle: **1500 MHz** (max 1800 MHz per config)

### Temperature

- **48.8 – 49.9 °C** (idle)

### Memory

```
Total:       7.9 GiB
Used:        2.2 GiB
Buff/cache:  3.9 GiB
Available:   5.7 GiB
Swap used:   72 MiB / 2.0 GiB
```

### RSS per process (idle)

| Process | RSS |
|---------|-----|
| rover2-api (`/opt/rover2/main.py`) | **94.9 MB** |
| tailscaled | 65.7 MB |
| rover-camera (`/opt/rover/`) | 49.8 MB |
| ble_scanner (`/opt/rover/`) | 21.4 MB |
| NetworkManager | 19.7 MB |
| bluetoothd | 6.3 MB |

### Disk

| Path | Used |
|------|------|
| `/` (SSD, 917 GB) | 34 GB (4%) |
| `/opt/rover2/` | 834 MB |
| `/home/ambassad0r/` | 12 GB |
| `/var/log/` | 2.3 MB |

### PMIC ADC (clean idle, ollama stopped)

| Rail | Current | Voltage | Power |
|------|---------|---------|-------|
| VDD_CORE | 0.902 A | 0.843 V | **0.761 W** |
| 0V8_SW | 0.275 A | 0.800 V | 0.220 W |
| 1V1_SYS | 0.355 A | 1.107 V | 0.393 W |
| 1V8_SYS | 0.159 A | 1.818 V | 0.289 W |
| 3V3_SYS | 0.101 A | 3.319 V | 0.335 W |
| 3V7_WL_SW (wlan0) | 0.104 A | 3.658 V | 0.381 W |
| EXT5V | — | 4.937 V | — |

No `EXT5V_A` channel in PMIC — total board input current not directly measurable.

---

## Step 2 — rover2-api Memory Profile (idle)

```
VmPeak:   1,099,632 kB  (1073 MB virtual peak)
VmSize:   1,036,032 kB  (1011 MB virtual current)
VmRSS:       97,152 kB  (94.9 MB resident) ✓ <150 MB target
VmHWM:       97,152 kB
VmSwap:           0 kB
Threads:         13
```

**smaps_rollup:**

| Metric | Value |
|--------|-------|
| Rss | 97,152 kB |
| Pss | 85,905 kB (83.9 MB private-equivalent) |
| Pss_Anon (heap) | 64,832 kB (63.3 MB) |
| Pss_File (mapped libs) | 21,073 kB (20.6 MB) |
| Private_Dirty | 64,912 kB |
| Shared_Clean | 15,024 kB |
| KSM / Swap | 0 |

**Top Pss contributors:**

| Library | Pss |
|---------|-----|
| python3 interpreter | 27.5 MB |
| _sqlite3 | 4.3 MB |
| _pyhailort | 4.2 MB |
| _pydantic_core | 3.9 MB |
| libhailort | 2.4 MB |
| libscipy_openblas64 | 2.0 MB |
| libcrypto | 1.5 MB |

---

## Step 3 — Detect-Only Mode

Hailo active, 1 fps inference, no drive commands.  
`tracking_hailo_ready: true` after 30 s warmup.

### CPU (real-time, sampled 40–60 s after enable)

| Sample | rover2-api | rover-camera† |
|--------|-----------|--------------|
| 1 | 5.4% | 8.6% |
| 2 | 5.8% | 8.6% |
| 3 | 6.2% | 8.6% |

**rover2-api detect-only avg: ~5.8%**

### Memory

```
VmRSS:   147,648 kB  (144.2 MB)  +49.3 MB vs idle
VmHWM:   147,696 kB
VmSwap:        0 kB
```

Memory does **not** shed after tracking stops — Hailo runtime stays resident (~145 MB HWM thereafter).

### Temperature

**47.7 °C** (detect-only, no person in frame)

### PMIC ADC (detect-only)

| Rail | Current | Voltage | Power | vs Idle |
|------|---------|---------|-------|---------|
| VDD_CORE | 2.134 A | 0.843 V | **1.799 W** | +1.038 W |
| 0V8_SW | 0.297 A | 0.799 V | 0.237 W | +0.017 W |
| 1V1_SYS | 0.355 A | 1.108 V | 0.393 W | ≈ 0 |
| 3V7_WL_SW | 0.107 A | 3.670 V | 0.393 W | +0.012 W |

**Hailo-10H detect-only overhead: +~1.1 W on VDD_CORE** (sampled during active frame).  
Value varies with Hailo duty cycle at 1 fps (~100 ms burst / ~900 ms idle).

---

## Step 4 — Full Follow Mode

Full follow requires an active WebSocket client sending heartbeat pings (`heartbeat_timeout_s: 10.0`).  
Without a connected client, the heartbeat guard fires after 10 s and disables tracking.  
CPU and Hailo load **identical to detect-only** when no person is in frame (same inference path).

---

## Step 5 — LLM Inference (CPU, llama3.2:1b)

hailo-ollama disabled. CPU fallback model: `llama3.2:1b` (1.3 GB on disk).  
Available models: `llama3.2:1b`, `llama3.2:3b`, `deepseek-r1:1.5b`, `gemma2:2b`.  
Note: `qwen2.5-instruct:1.5b` is hailo-ollama only (not in regular ollama).

### Response latency

| Call | Total | Load | Prompt eval | Generation |
|------|-------|------|-------------|------------|
| Cold (first call, model load) | 4,230 ms | 2,758 ms | 1,311 ms | ~161 ms (2 tokens) |
| Hot (model cached in runner) | 1,531 ms | 428 ms | 807 ms | ~141 ms (3 tokens) |

### CPU during inference

Peak: **100%** on one core (arm saturated). Trailing samples: 77.9%, 63.8% as generation winds down.

### Memory

| Process | RSS |
|---------|-----|
| ollama runner (llama3.2:1b loaded) | 1,479 MB |
| ollama serve | 117.2 MB |
| rover2-api (unchanged) | ~145 MB |

**Total system memory during LLM: ~1.74 GB additional** (model fully in RAM).

### Temperature

**53.8 °C** during LLM inference.

---

## Step 6 — VLM Inference

**N/A.** hailo-ollama disabled; no CPU vision model installed in ollama.  
Hailo VLM (via hailo-ollama) when re-enabled: not measured in this session.

---

## Step 7 — faster-whisper STT (tiny INT8)

Model: `faster-whisper tiny` (int8, ctranslate2). Endpoint: `POST /api/voice/transcribe`.  
Test audio: 2 s silence WAV, 16 kHz mono 16-bit.

### Memory delta

| State | rover2-api RSS |
|-------|---------------|
| Before first transcription call | 145.8 MB |
| After model load (cold call) | **402.9 MB** |
| Delta | **+257 MB** |

**After whisper loads, rover2-api exceeds the 280 MB watchdog WARNING threshold.**

**Resolved (2026-06-07):** whisper auto-unloads after 5 min inactivity (`voice.whisper_unload_after_s: 300`).
Watchdog thresholds are now whisper-aware: +280 MB added to all thresholds when model is loaded.

### Latency

| Call | Latency |
|------|---------|
| Cold (model load + inference) | **24,857 ms = 24.9 s** |
| Hot (model resident) | **9,510 ms = 9.5 s** |
| First response after unload (reload) | **~25 s** (same as cold — model must reload) |

Both measured on 2 s silence audio. Actual speech would be similar (silence is not skipped).

### Auto-unload behaviour

| State | RSS |
|-------|-----|
| Idle, whisper unloaded | ~95 MB (base) |
| During/after voice use (whisper loaded) | ~350–420 MB (normal for up to 5 min post-voice) |
| After auto-unload (5 min idle) | ~95 MB (GC reclaims ~257 MB) |

### Watchdog interaction

Original: watchdog detected 403 MB RSS and auto-stopped ollama (`memory_high_403MB`).  
Fixed (2026-06-07): thresholds raised by +280 MB when whisper is loaded — no spurious alerts
during legitimate whisper-loaded periods. Whisper auto-unloads after 5 min returning RSS to baseline.

### Temperature

**55.4 °C** post-STT (peak observed in session).

---

## Step 8 — Watchdog Cycle Overhead

- Interval: 30 s, in-process asyncio task (no separate process)
- CPU overhead: **< 0.5%** (absorbed into rover2-api idle measurement, not separately visible)
- Actions logged during session:
  - `memory_high_403MB → stop_ollama → ok` (triggered by whisper + ollama combination)
  - `ollama_runners_2 → restart_ollama` (from earlier session — multiple stale runners)
  - Action limit: 3 actions / 60 min window; halts further auto-fix on breach

---

## Step 9 — wlan1 AP Power Delta

wlan1 is disabled on mains power per config policy. Brief measurement taken.

| State | 3V7_WL_SW_A | Delta |
|-------|-------------|-------|
| wlan1 DOWN | 0.108 A | baseline |
| wlan1 UP, no carrier | 0.111 A | +0.003 A ≈ **+11 mW** |

Full hostapd AP mode (with SSID broadcast and client traffic) not measured — disabled on mains.

---

## Step 10 — 10-Minute Sustained Detect-Only

Duration: ~11 min (uptime 671 s at final measurement). No person in frame.

### CPU (real-time samples at ~10 min mark)

| Sample | rover2-api | rover-camera† |
|--------|-----------|--------------|
| 1 | 19.8% | 8.4% |
| 2 | 19.9% | 8.4% |
| 3 | 19.9% | 8.4% |

Note: `ps aux %CPU` is lifetime average (total CPU time / elapsed time). rover2-api shows ~20% lifetime average because startup + Hailo warmup are CPU-heavy. Real-time detect-only inference load is ~6% (see Step 3). Startup overhead decays toward Step 3 rate over long sessions.

### Memory (at 10 min)

```
VmRSS:  149,088 kB  (145.6 MB)  — stable, no growth
VmHWM:  149,264 kB
VmSwap:       0 kB
```

### Temperature (at 10 min)

**48.8 °C** — stable, matches idle. No thermal runaway.

### Throttle status

```
throttled=0x50000
```

- Bit 16 SET: under-voltage **occurred** (historical, earlier session)
- Bit 18 SET: throttle **occurred** (historical, earlier session)
- Bits 0–3: **all clear** — not throttled now

No active throttling during sustained detect-only session.

### PMIC ADC (at 10 min, during inference burst)

| Rail | Current | Voltage | Power |
|------|---------|---------|-------|
| VDD_CORE | 1.456 A | 0.793 V | **1.154 W** |
| 0V8_SW | 0.389 A | 0.801 V | 0.312 W |
| 1V1_SYS | 0.357 A | 1.111 V | 0.396 W |
| 3V3_SYS | 0.110 A | 3.312 V | 0.364 W |
| 3V7_WL_SW | 0.108 A | 3.652 V | 0.394 W |
| EXT5V | — | 4.967 V | — |

VDD_CORE lower than Step 3 (1.154 W vs 1.799 W) — measured between Hailo bursts at 1 fps duty cycle.

---

## Summary Table

| Scenario | rover2-api RSS | CPU (real-time) | VDD_CORE | Temp |
|----------|---------------|-----------------|----------|------|
| Idle (no tracking) | **94.9 MB** | **~2%** | 0.761 W | 48–50 °C |
| Detect-only (Hailo active) | **144–149 MB** | **~6%** | 1.2–1.8 W | 48–50 °C |
| + LLM (llama3.2:1b, hot) | +117 MB (ollama serve) +1479 MB (runner) | +100% peak | +CPU | 54 °C |
| + whisper STT (after load) | **+257 MB → ~403 MB total** | high during inference | +CPU | 55 °C |

## Key Findings and Risks

1. **rover2-api idle RSS: 94.9 MB** — within 150 MB target ✓
2. **Hailo loads +49 MB** (94.9 → 145 MB) on first detect-only — stays resident thereafter
3. **whisper loads +257 MB** (145 → 403 MB) — exceeds watchdog WARN (280 MB); **resolved 2026-06-07**: auto-unloads after 5 min; watchdog thresholds are now whisper-aware (+280 MB when loaded)
4. **LLM runner: 1.5 GB RSS** — fills most of available RAM; cannot run concurrently with whisper
5. **rover-camera (`/opt/rover/`)** runs constantly at 8.6% CPU — old project, not rover2; should be investigated/stopped if not needed
6. **Throttle history**: past undervolt/throttle events (0x50000). Currently clean. Likely from high-load sessions on USB-C 3A cable.
7. **Full follow mode**: requires active WebSocket client; heartbeat guard prevents autonomous unsupervised driving ✓
8. **Temperature**: all scenarios ≤ 55.4 °C; no active throttling during sustained detect session
9. **wlan1 AP**: +11 mW at radio-up (no carrier) — negligible

## Notes

- `EXT5V_A` (total board input current) has no PMIC channel — cannot derive total power from Pi alone
- PMIC VDD_CORE varies with Hailo burst timing at 1 fps; instantaneous vs average reads differ significantly  
- hailo-ollama (Hailo LLM, port 8000) not measured — disabled for this session  
- VLM (hailo-ollama) not measured — disabled  
- Sustained detect test note: `pgrep` captured stale PID immediately post-restart; corrected to PID 50320 for final measurements
