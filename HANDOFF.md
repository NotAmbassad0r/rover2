# HANDOFF.md — ROVER2

Last updated: 2026-05-16 (follow logging + safety rate-limit; ready to test FOLLOW on robot)

Greenfield minimal stack: MegaPi motors, **arm lift**, gripper, ultrasonic, web control, **Hailo person follow**. Runs **alongside** ROVER v1 on a separate port; **do not** bind both APIs to `/dev/ttyUSB0` at once.

**Claude Code:** Start every session with: `Read HANDOFF.md and PROJECT.md in full before making changes.`

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

### SSH

```bash
ssh ambassad0r@192.168.70.11       # eth0 (preferred)
ssh rover-eth                      # ~/.ssh/config → 192.168.70.11
ssh ambassad0r@192.168.250.254     # WiFi
ssh rover                          # ~/.ssh/config → WiFi
```

---

## Phase status

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 0 | Motors, gripper, ultrasonic, web D-pad | **Done** |
| 1 | Forward obstacle stop (ultrasonic &lt; 40 cm) | **Verified** |
| 2 | WebSocket D-pad, live telemetry, stop on disconnect | **Done** |
| 2b | Arm lift (PORT3B), web + API | **Done** (firmware 1.0.3) |
| 3 | Camera / Hailo person follow (FOLLOW toggle) | **Enabled — tune on robot** |
| 4 | BLE beacon + follow (Flip 6) | **Deferred** (needs new Flip 6 / A32 apps) |
| 5+ | Android, extensions | Out of scope here |

Full roadmap: `PROJECT.md` (phase numbers in PROJECT.md still list BLE before camera historically; **implementation order was camera first**).

---

## Architecture

```
Browser (index.html)  http://192.168.70.11:8082/
    → WebSocket ws://<pi>:8082/ws     drive, grip, arm, tracking, telemetry
    → HTTP REST /api/*                fallback + firmware flash
    → Camera preview                  http://<pi>:8081/stream  (rover-camera.service)
        → pi/server.py + ws_control.py + safety.py
        → pi/body_tracker.py          Hailo YOLOv8m person follow (optional)
        → pi/megapi.py                  pyserial, ultrasonic poll
        → /dev/ttyUSB0 @ 115200
        → firmware/rover2_basic/        rover2-basic-1.0.3
            PORT1B/2B drive, PORT3B arm, PORT4B gripper, ultrasonic auto-scan
```

| Service | Port | Notes |
|---------|------|--------|
| `rover2-api.service` | **8082** | ROVER2 control + follow |
| `rover-camera.service` | **8081** | MJPEG (v1 stack; shared) |
| `rover-api` (v1) | 8080 | Stop when testing ROVER2 serial |
| `hailo-ollama` (v1) | 8000 | May contend for AI HAT — stop to debug follow |

---

## What works (confirmed 2026-05-16)

- D-pad tuned for Ultimate 2.0 (`drive.direction_map`: forward `(1,-1)`, etc.)
- Gripper open/close (PORT4B)
- **Arm lift** hold up/down + nudge pulse (PORT3B; firmware 1.0.3)
- Ultrasonic on **PORT_8** (firmware auto-scan; logs `ultrasonic on PORT_8`)
- Live ultrasonic via WebSocket telemetry
- Safety: forward blocked when distance &lt; 40 cm; rate-limited warn (2 s)
- WebSocket: drive, stop, grip, arm, tracking; stop on disconnect
- **Live camera** in UI (loads `:8081/stream` on same hostname)
- **FOLLOW** stack fully loaded (`tracking_available: true`, `tracking_hailo_ready: true`)
  — journalctl now shows `tracking ENABLED/DISABLED`, `person ACQUIRED/LOST`, follow dir @ DEBUG
- Firmware flash: `./scripts/flash_firmware.sh` (default Pi **192.168.70.11**)
- `deploy_pi.sh` → rsync `pi/`, `scripts/`, link Hailo, optional systemd refresh

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
# ROVER2_PI_HOST=192.168.250.254 ./scripts/flash_firmware.sh
```

Stops `rover2-api`, flashes via `/opt/rover2/tools/avrdude`, restarts service.

**Required after arm feature** if Pi still reports `rover2-basic-1.0.2` in `/api/status`.

### Arduino compile (central-computer)

```bash
~/.local/bin/arduino-cli compile --fqbn arduino:avr:mega \
  --build-path /tmp/rover2_fw_build \
  ~/Documents/projects/rover2/firmware/rover2_basic
```

---

## Deploy

```bash
cd ~/Documents/projects/rover2
./deploy_pi.sh
```

- Auto-picks first reachable host: `rover-eth`, `192.168.70.11`, `rover`, WiFi, Tailscale.
- Default fallback IP: **192.168.70.11**.
- Runs `scripts/link_hailo_for_rover2.sh` on Pi (symlinks system `hailo_platform` into ROVER2 venv).
- Updates `systemd/rover2-api.service` if present in repo.
- **`pip install httpx` often fails** on Pi (no internet) — OK; camera uses **8081** directly.

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
# Expect: firmware rover2-basic-1.0.3, tracking_available true
```

---

## Web UI

**http://192.168.70.11:8082/** (WiFi: http://192.168.250.254:8082/)

Hard-refresh after deploy: `Ctrl+Shift+R`.

| Section | Notes |
|---------|--------|
| STATUS | WEBSOCKET, SERIAL, ULTRASONIC, SAFETY, FOLLOW, PERSON |
| LIVE CAMERA | MJPEG from port **8081** (not `/stream` on 8082 unless httpx installed) |
| ARM LIFT | Hold ▲/▼; NUDGE = short pulse |
| DRIVE | D-pad; disabled while FOLLOW on |
| FOLLOW | Needs `tracking_available`; uses Hailo + camera |

---

## WebSocket protocol

Endpoint: `ws://192.168.70.11:8082/ws`

Docs: `docs/WEBSOCKET.md`

**Client → server:** `drive`, `stop`, `grip`, `arm`, `arm_pulse`, `tracking`, `ping`  
**Server → client:** `telemetry`, `ack`, `pong`, `error`

Telemetry includes: `tracking_available`, `tracking_enabled`, `tracking_hailo_ready`, `person_detected`, optional `tracking_error`.

---

## REST API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/status` | Full snapshot |
| POST | `/api/drive` | Drive (HTTP fallback) |
| POST | `/api/stop` | Stop all motors + arm |
| POST | `/api/grip` | `{"action":"open"\|"close"}` |
| POST | `/api/arm` | `{"direction":"up","speed":1}` or `{"action":"up"}` pulse |
| POST | `/api/tracking` | `{"enabled":true\|false}` |
| GET | `/api/ultrasonic` | Force one read |
| GET | `/stream` | MJPEG proxy (503 if `httpx` missing — use :8081) |
| POST | `/api/firmware/upload` | Stage hex |
| POST | `/api/firmware/flash` | avrdude flash |

Serial protocol: `docs/PROTOCOL.md`

---

## Config highlights (`pi/config.yaml`)

```yaml
server:
  port: 8082

drive:
  # direction_map:   # forward: [1, -1]  etc.

arm:
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
```

Chassis / follow tuning: `drive.direction_map`, `body_tracker.*` in same file.

---

## Hailo / camera follow

| Item | Location |
|------|----------|
| Model | `/opt/rover/models/yolov8m_h10.hef` (shared with v1) |
| Python | System `hailo_platform` → linked into ROVER2 venv by `scripts/link_hailo_for_rover2.sh` |
| Code | `pi/body_tracker.py`, `pi/body_tracker_parse.py` |
| Tests (no Hailo) | `pi/tests/test_body_tracker.py` |

**Do not set `PYTHONPATH=/usr/lib/python3/dist-packages` in systemd** — breaks FastAPI/pydantic (`typing_extensions` conflict). Use the link script only.

If FOLLOW misbehaves or Hailo init fails:

```bash
ssh rover-eth "sudo systemctl stop hailo-ollama.service"
ssh rover-eth "sudo systemctl restart rover2-api.service"
# test FOLLOW, then optionally restart hailo-ollama
```

---

## Relation to ROVER v1

| | ROVER v1 | ROVER2 |
|---|----------|--------|
| Repo | `~/Documents/projects/rover/` | `~/Documents/projects/rover2/` |
| Pi path | `/opt/rover/` | `/opt/rover2/` |
| API | :8080 | :8082 |
| Serial | `/dev/ttyUSB0` | same — **only one stack at a time** |

```bash
ssh rover-eth "sudo systemctl stop rover-api.service"      # for ROVER2
ssh rover-eth "sudo systemctl stop rover2-api.service"     # for v1
```

v1 context: `~/Documents/projects/rover/HANDOFF.md`

---

## Key files

| Path | Role |
|------|------|
| `pi/main.py` | Entrypoint, SafetyMonitor, BodyTracker, uvicorn |
| `pi/server.py` | FastAPI, `/ws`, camera proxy (optional) |
| `pi/ws_control.py` | WebSocket hub |
| `pi/megapi.py` | Serial bridge |
| `pi/safety.py` | Ultrasonic forward gate |
| `pi/arm_control.py` | Arm PWM mapping |
| `pi/body_tracker.py` | Hailo person follow |
| `pi/body_tracker_parse.py` | YOLO NMS parsing (unit tests) |
| `pi/camera_proxy.py` | `/stream` proxy (needs httpx) |
| `pi/web/static/index.html` | Web UI |
| `firmware/rover2_basic/` | Active firmware |
| `scripts/flash_firmware.sh` | Compile + flash |
| `scripts/link_hailo_for_rover2.sh` | Hailo symlink into ROVER2 venv |
| `scripts/install_avrdude_on_pi.sh` | One-time avrdude in `/opt/rover2/tools/` |
| `deploy_pi.sh` | Deploy + Hailo link + restart |
| `systemd/rover2-api.service` | systemd unit |
| `docs/PROTOCOL.md` | Serial JSON |
| `docs/WEBSOCKET.md` | WebSocket types |

Legacy (do not flash): `firmware/rover2_firmware/`

---

## Known issues / notes

1. **Ultrasonic** — Auto-scan locks port; power-cycle MegaPi to rescan if sensor moved.
2. **MakeBlock timeout** — `distanceCm(N)` returns N on timeout; firmware rejects ≥400 cm.
3. **httpx** — Removed from `requirements.txt` (Pi has no internet; optional). Camera UI uses **:8081**; `/stream` on 8082 returns 503 without httpx.
4. **Hailo contention** — `hailo-ollama.service` (v1) is active; body_tracker uses `ROUND_ROBIN` group_id=`rover2` which should multiplex, but stop hailo-ollama first if follow won't start or Hailo init loops.
5. **Pi TCP/443 blocked at gateway** — Ping (ICMP) and DNS work fine; TCP port 443 (HTTPS) is silently dropped by the `192.168.70.1` gateway. `pip install` therefore fails for any package not already cached, even though `ping google.com` succeeds. IPv6 has no route at all. Fix: `deploy_pi.sh` now sideloads `httpx` from the dev machine via `scp` if pip can't install it. Pip timeout set to 5 s in `~/.config/pip/pip.conf` on Pi to avoid long hangs.
6. **restart-loop after deploy** — `pip install` during deploy can briefly remove packages (typing_extensions). `Restart=on-failure` retries twice and recovers. Harmless; service is stable after 3rd start.
7. **FOLLOW vs manual drive** — D-pad disabled while FOLLOW on; manual drive disables FOLLOW.

---

## Next session — recommended work

**Primary: tune camera follow on robot (Phase 3)**

```bash
# Step 1 — free Hailo if follow init loops
ssh rover-eth "sudo systemctl stop hailo-ollama.service"

# Step 2 — watch live follow logs
ssh rover-eth "journalctl -u rover2-api -f"
# In browser: http://192.168.70.11:8082/ → FOLLOW ON with person in frame
# Expect: "person ACQUIRED", direction lines, "person LOST" when leaving frame
# Enable DEBUG to see per-frame decisions:
#   ssh rover-eth "sudo journalctl -u rover2-api -f --output=short-monotonic"
# (uvicorn runs at INFO; body_tracker Follow: lines are at DEBUG — not visible by default)
# To see DEBUG: add --log-level debug to ExecStart in systemd unit temporarily.

# Step 3 — tune if needed (edit pi/config.yaml, then ./deploy_pi.sh)
#   turn_speed: 100   → lower (80) if turns overshoot
#   forward_speed: 120 → lower (90) if advance too fast
#   target_bbox_width: 0.35 → higher (0.45) to stop farther away
#   centre_zone: 0.30 → higher (0.40) for looser centering

# Step 4 — confirm safety blocks forward near obstacles
# Hold rover facing wall at < 40 cm with FOLLOW ON — robot should not advance.
```

**Later: BLE follow (Phase 4)**

- Requires new Flip 6 beacon app + decision on Pi vs phone RSSI (v1 used A32).
- UUID ref: `6a8e5b1c-f2d4-4e3a-9c7b-0d1f2e3a4b5c` in v1 HANDOFF.

**Optional polish**

- Install `httpx` when Pi has internet (enables `/stream` proxy on 8082).
- WebSocket drive heartbeat while held.
- Lock ultrasonic port in firmware once hardware is final.

### Suggested Claude Code prompt

```
Read HANDOFF.md and PROJECT.md in rover2/ in full.

Continue ROVER2 camera follow tuning:
- Verify FOLLOW on http://192.168.70.11:8082/
- Adjust body_tracker and safety interaction as needed
- Deploy with ./deploy_pi.sh; flash only if firmware changes

Report what you changed and how follow behaved on the robot.
```

---

## Versioning

**Repo:** https://github.com/NotAmbassad0r/rover2

**Branches:**
- `main` — stable, tagged releases only
- `dev` — active development (you are here during dev phase)

**Tags / releases (semver):**

| Tag | Meaning |
|-----|---------|
| `v0.3.0` | Phase 3 follow enabled — dev phase (current) |
| `v0.3.x` | Follow tuning patches |
| `v1.0.0` | Phase 3 verified untethered — test1 pass → merge dev→main |
| `v1.1.0` | Phase 4: BLE beacon follow |

**Phases:**
- **dev phase** — robot on eth0 (192.168.70.11) + power cables; limited movement
- **test1 phase** — robot untethered (WiFi 192.168.250.254, battery); full mobility test

**Workflow:**
```bash
# daily work on dev branch
git checkout dev
# ... make changes, deploy, test ...
git add -p && git commit -m "fix: ..."
git push

# patch release after tuning
git tag -a v0.3.1 -m "follow tuning: lower turn_speed, wider centre_zone"
git push origin v0.3.1
gh release create v0.3.1 --generate-notes

# test1 milestone → merge to main
git checkout main && git merge --no-ff dev
git tag -a v1.0.0 -m "Phase 3 verified untethered (test1)"
git push origin main v1.0.0
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
