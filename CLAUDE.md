# CLAUDE.md — ROVER2

**Start every session: `Read HANDOFF.md in full before making changes.`**

## Project priorities (non-negotiable order)

1. **Energy / resource efficiency** — idle = zero CPU. No polling, no spin loops when inactive.
2. **Performance** — speed and responsiveness of what is already built.
3. **Features** — only after 1 and 2 are satisfied.

## Hard rules

- Hailo does all inference. Never run vision/ML on CPU.
- CPU governor: `schedutil` always.
- rover2-api memory target: <150 MB RSS.
- `stress-ng` / virtual USB dongle: only on battery, always off on mains.

## Layout

| Path | Purpose |
|------|---------|
| `pi/` | Service code (deployed to `/opt/rover2/`) |
| `pi/config.yaml` | All runtime tuning — edit here, deploy via `deploy_pi.sh` |
| `pi/web/static/index.html` | Single-page control UI |
| `systemd/` | Service unit files |
| `scripts/` | Deploy / setup helpers |
| `docs/` | Test plans, results, roadmap |

## Pi access

```bash
ssh ambassad0r@192.168.70.11      # eth0 (cable) — always preferred
ssh ambassad0r@192.168.250.254    # WiFi — only from devices on local WiFi
ssh ambassad0r@100.67.13.10       # Tailscale
```

Deploy: `./deploy_pi.sh`  
Logs: `ssh ambassad0r@192.168.70.11 journalctl -u rover2-api -f`

## Current branch: `dev`

Push to GitHub with `git push origin dev`. PR to `main` only for stable milestones.

## Key config values (pi/config.yaml)

```yaml
drive:
  max_speed: 255
  direction_map:
    forward: [1, -1]
    back: [-1, 1]
    left: [1, 0]       # arc turn — left motor only (fixed 2026-05-26)
    right: [0, -1]     # arc turn — right motor only (fixed 2026-05-26)
body_tracker:
  turn_speed: 210
  forward_speed: 170
  frame_interval_s: 0.25   # 4 fps — power cap
  hailo_warmup_s: 30.0     # lazy init delay
websocket:
  heartbeat_timeout_s: 10.0
```

## HTTPS (as of 2026-05-26)

`rover2-api` runs on **HTTPS** (self-signed cert). Always use `https://`:

```
https://192.168.250.254:8082/   # WiFi
https://192.168.70.11:8082/     # eth0
```

Certs live at `/opt/rover2/rover.key` and `/opt/rover2/rover.crt` (generated once, not in repo).
First visit: browser will warn about self-signed cert — click **Advanced → Proceed**.
The UI auto-upgrades WebSocket to `wss://` when served over HTTPS.

## Power situation (as of 2026-05-22)

Viking PN-964PD bank + 3A USB-C cable = 15W ceiling. Pi 5 + Hailo needs 18-20W.
**Real fix**: 5A/100W e-marked cable ordered. Until it arrives, Hailo loads lazily (30s warmup).
Mitigations in place: lazy init, 4fps, CPU cap 1800MHz, `arm_freq=1800` in `/boot/firmware/config.txt`.

## Boot partition

`/boot/firmware` is mounted **read-only** in `/etc/fstab` (`ro,defaults`) to prevent cmdline.txt corruption on hard power cut. Before firmware updates: `sudo mount -o remount,rw /boot/firmware`.

## WebSocket API (port 8082 — HTTPS/WSS)

> API runs on **HTTPS**. Use `https://` in browser and `curl -k`. WebSocket is `wss://`.
> Self-signed cert at `/opt/rover2/rover.crt`. Accept browser warning once on first visit.

| Message | Effect |
|---------|--------|
| `{type: "drive", direction: "forward", speed: 1.0}` | Drive |
| `{type: "stop"}` | Stop motors |
| `{type: "tracking", enabled: true}` | Enable person follow |
| `{type: "tracking", detect_only: true}` | Detect without driving |
| `{type: "ping"}` | Keepalive → pong |

REST: `POST /api/tracking` — body `{enabled, detect_only, ble_follow_enabled}`

## BLE fallback

Galaxy Z Flip 6 (UUID `0000fcf1-…`) auto-activates as beacon when camera loses person for 2+ seconds. Toggle via BLE button in UI or `POST /api/tracking {"ble_follow_enabled": false}`.

For **office demo** (no BLE needed): set `ble_tracker.enabled: false` in `pi/config.yaml` → deploy.

## Voice assistant (fully local — no cloud)

- **STT**: faster-whisper tiny (int8, ctranslate2) on Pi — `/api/voice/transcribe` + `/api/voice/wake`
- **Wake word**: A32 VAD (AnalyserNode RMS) → 2.5 s chunk → `/api/voice/wake` → "rover" detected
- **TTS**: Web Speech API on A32 for responses; Piper for proactive events (PERSON_FOUND etc)
- **`webkitSpeechRecognition` is NOT used** — it requires Google servers (breaks offline demo)
- Key constants in `face/index.html`: `WAKE_RMS_THRESHOLD`, `CONV_RMS_THRESHOLD`, `WAKE_CHUNK_S`
