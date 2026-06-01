# ROVER2

Minimal rebuild of the ROVER robot stack: **motors**, **gripper**, **ultrasonic**, and a **web D-pad** — no Android, vision, or LLM.

## Repository layout

```
rover2/
├── PROJECT.md              # Requirements, hardware, phases
├── README.md               # This file
├── deploy_pi.sh            # rsync pi/ → Pi /opt/rover2/
├── docs/
│   └── PROTOCOL.md         # MegaPi JSON serial protocol
├── firmware/
│   └── rover2_firmware/    # Arduino sketch (Makeblock MegaPi)
├── pi/
│   ├── main.py             # Entrypoint
│   ├── server.py           # FastAPI routes
│   ├── megapi.py           # Serial bridge
│   ├── config.yaml
│   ├── requirements.txt
│   └── web/static/index.html
└── systemd/
    └── rover2-api.service
```

## Quick start (central-computer)

### 1. Flash firmware (once)

Requires `arduino-cli` and Makeblock `MeMegaPi` library (same as ROVER v1).

```bash
arduino-cli compile --fqbn arduino:avr:mega \
  --build-path /tmp/rover2_fw_build \
  ~/Documents/projects/rover2/firmware/rover2_firmware
```

Flash from the Pi via `avrdude` (see `firmware/README.md`) or USB from a machine connected to the MegaPi.

### 2. Pi setup (once)

```bash
ssh rover
sudo mkdir -p /opt/rover2
python3 -m venv /opt/rover2/venv
```

Copy env (optional):

```bash
# /etc/rover2.env
ROVER2_SERIAL_PORT=/dev/ttyUSB0
```

Install systemd:

```bash
sudo cp ~/Documents/projects/rover2/systemd/rover2-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rover2-api.service
```

ROVER2 uses port **8082** so it can run alongside ROVER v1 on 8080.

### 3. Deploy

```bash
cd ~/Documents/projects/rover2
chmod +x deploy_pi.sh
./deploy_pi.sh
# Default deploy target is eth0 <ROVER_ETH_IP>; override: ROVER2_PI_HOST=<ROVER_WIFI_IP> ./deploy_pi.sh
```

**Serial port:** only one service can use `/dev/ttyUSB0`. ROVER2 was started while `rover-api` was stopped. Do not run both against the MegaPi at once.

### 4. Control

Open in a browser: `http://<ROVER_ETH_IP>:8082/` (eth0; WiFi: `http://<ROVER_WIFI_IP>:8082/`).

- **WebSocket** (`/ws`): D-pad, gripper, live telemetry (~2.5 Hz). Motors stop if the tab disconnects.
- Hold D-pad directions; release to stop.
- Grip open/close, ultrasonic read, E-stop.
- **Safety (Phase 1):** forward drive blocked when ultrasonic &lt; 40 cm (configurable in `config.yaml`).

## API

| Method | Path | Body |
|--------|------|------|
| GET | `/api/status` | — |
| POST | `/api/drive` | `{"direction":"forward","speed":0.8}` |
| POST | `/api/stop` | — |
| POST | `/api/grip` | `{"action":"open"}` |
| GET | `/api/ultrasonic` | — |
| GET | `/api/firmware/tools` | avrdude installed on Pi? |
| POST | `/api/firmware/upload` | raw `.hex` or multipart `firmware` |
| POST | `/api/firmware/flash` | flash `/tmp/rover2_firmware.hex` (background) |

Directions: `forward`, `back`, `left`, `right`, `stop`.

## Firmware (MegaPi)

Sketch: `firmware/rover2_basic/` — fixed ports, no auto-detect (`rover2-basic-1.0.0`).

**From central-computer** (compile + flash over SSH):

```bash
./scripts/flash_firmware.sh                    # default Pi eth0 <ROVER_ETH_IP>
ROVER2_PI_HOST=<ROVER_WIFI_IP> ./scripts/flash_firmware.sh   # WiFi fallback
```

**First time on a Pi** — install avrdude under `/opt/rover2/tools`:

```bash
ssh rover 'bash -s' < scripts/install_avrdude_on_pi.sh
```

**From the Pi** (after uploading a hex to `/tmp/rover2_firmware.hex`):

```bash
curl -X POST http://127.0.0.1:8082/api/firmware/flash
# log: /tmp/rover2_flash.log
```

## Development

```bash
cd pi
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Connect MegaPi USB to the dev machine and set `config.yaml` `serial.port` accordingly.
