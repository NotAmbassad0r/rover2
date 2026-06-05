# ROVER2 Backlog

Items here are confirmed desirable but not yet scheduled.
Format: ## Category / ### Item / bullet details / Priority line.

---

## Web UI

### Improve web GUI and verify all functions

- Audit every tab (CONTROL, TOOLS, DIAG, CHAT, METRICS) for visual consistency
- Check every button, toggle, slider, and input actually works end-to-end
- Verify STATUS table values are live and accurate (WEBSOCKET, SERIAL, MOTORS, FIRMWARE,
  ULTRASONIC, SAFETY, FOLLOW, SOURCE, PERSON, BLE BEACON, CAMERA, UPTIME)
- Verify follow mode selector (DETECT / CAMERA / FUSED / BLE) transitions correctly
- Verify D-pad, arm lift, gripper all respond
- Verify METRICS tab charts update in real time
- Verify DIAG tab shows current values, not stale cache
- Verify CHAT tab agent responses and VLM button work
- Verify TOOLS tab scripts execute and return output
- Verify alerts appear in UI when watchdog fires
- Verify person detection overlay appears on camera feed when Hailo active
- Verify face PWA debug overlay and status text update correctly
- Verify speech bubble appears and dismisses on TTS
- Fix any broken, missing, or inconsistent UI elements found
- Ensure UI works on both desktop browser and mobile (Android Chrome)

Priority: medium — after watchdog expansion and HailoRT 5.2.0 upgrade

---

## Voice

### Increase TTS speech speed slightly

Both TTS paths speak slightly too slowly. Increase speed on both:

- Web Speech API (agent replies via A32): `rate` is currently 0.88 in face/index.html
  (_makeTtsUtterance) — increase to ~0.95
- Piper TTS (proactive events via voice_engine.py): `length_scale` is currently 1.05
  in voice_engine.py — decrease to ~0.92 (lower = faster in Piper)

Test both after change:
- Web Speech: trigger a voice conversation, listen to agent reply speed
- Piper: trigger a proactive event (e.g. person found) or POST /api/voice/speak

Tune further if still too slow or becomes too fast. Values are in config.yaml if
already externalised; otherwise update the constants directly and consider moving
them to config.yaml as part of this task.

Priority: low

---

## AI / Voice

### Re-enable hailo-ollama alongside VLM (GenAI session multiplexing)

HailoRT 5.2.0 firmware only supports one GenAI session at a time. hailo-ollama
(voice agent LLM fast-path, ~3s) and VLM (scene description) cannot run simultaneously.
Current state: hailo-ollama disabled, voice agent falls back to CPU llama3.2:1b (~20-30s).

Options to investigate:
1. **Time-multiplexed sessions** — rover2-api opens a GenAI session per request and
   closes it immediately after, rather than holding it open. Requires changes to
   hailo-ollama and vlm_engine.py session lifecycle. Risk: session open/close overhead
   may add latency.
2. **Request queue with single session token** — a shared asyncio lock that hailo-ollama
   and vlm_engine.py both acquire before opening a GenAI session. One runs at a time,
   other waits. Simple but adds latency when both are requested concurrently.
3. **Hailo firmware update** — monitor Hailo developer zone for 5.x firmware that lifts
   the single-session limit. No code change needed if this lands.
4. **Dedicated session per model at startup, shared via ROUND_ROBIN** — check if
   ROUND_ROBIN scheduler used by body tracker applies to GenAI sessions in 5.2.0.
   May not — body tracker uses VDMA not GenAI.

Impact: voice agent response time 20-30s vs 3s. High priority for demo quality.
Priority: high
