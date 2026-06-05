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
