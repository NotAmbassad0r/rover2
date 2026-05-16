# ROVER2 — project definition

Greenfield rewrite of the ROVER companion robot, scoped to **Phase 0: motion + sensors + web control** before Android, vision, LLM, or extensions.

## Goals (this repo)

1. Drive chassis from a browser D-pad (hold-to-move, release-to-stop).
2. Open/close gripper (DC motor).
3. Read ultrasonic distance (cm).
4. Reliable MegaPi USB serial bridge on Raspberry Pi.
5. Single deploy path: `deploy_pi.sh` → `/opt/rover2/`.

## Out of scope (later phases)

- Android brain, face, voice, BLE follow
- AI HAT / camera tracking / LLM
- Extension plugin system
- Flip 6 companion app
- Home Assistant / patrol / safety fusion (beyond basic stop)

## Hardware (from ROVER inventory)

| Component | Role |
|-----------|------|
| Raspberry Pi 5 8GB | Host FastAPI + serial bridge |
| Makeblock MegaPi (Arduino Mega) | Motor + sensor controller |
| Ultimate 2.0 chassis | Left PORT1B, right PORT2B |
| Gripper DC motor | PORT4B |
| Ultrasonic (RJ25) | Auto-detect on first read |
| USB serial | `/dev/ttyUSB0` typical |

### Network (reference)

| Device | WiFi | eth0 | Tailscale |
|--------|------|------|-----------|
| Pi `rover` | 192.168.250.254 (WiFi) | **192.168.70.11** (eth0, preferred) | 100.67.13.10 |
| Dev `central-computer` | — | — | 192.168.20.11 |

## Software architecture

```
Browser (D-pad UI)
    → WebSocket + HTTP :8082 (FastAPI)
        → megapi.py (pyserial)
            → MegaPi firmware (JSON lines)
                → Motors / gripper / ultrasonic
```

## Phase roadmap

| Phase | Deliverable |
|-------|-------------|
| **0** (done) | Motors, gripper, ultrasonic, web D-pad |
| **1** (done) | Obstacle stop on forward (ultrasonic threshold) |
| **2** (done) | WebSocket D-pad, live telemetry, stop on disconnect |
| 3 | BLE beacon + follow (Flip 6) |
| 4 | Camera tracking (Hailo) |
| 5 | Android face + voice |
| 6 | Extensions platform |

## Paths

| Machine | Path |
|---------|------|
| Dev | `~/Documents/projects/rover2/` |
| Pi | `/opt/rover2/` |

## Relation to ROVER v1

`~/Documents/projects/rover/` remains the full system. ROVER2 reuses the same MegaPi wiring and compatible firmware protocol; code is intentionally small and readable.
