# ROVER2 MegaPi firmware

Arduino sketch for the Makeblock MegaPi (ATmega2560). Protocol matches ROVER v2.1.3 so you can also keep existing firmware until you flash ROVER2.

## Wiring (hardcoded)

| Function | Port |
|----------|------|
| Left motor | PORT1B |
| Right motor | PORT2B |
| Gripper open/close | PORT4B (DC, 600 ms pulse) |
| Arm lift up/down | PORT3B (DC, hold or 800 ms pulse) |

Ultrasonic: auto-scanned on RJ25 ports at first `sensor_req`.

Active sketch: `firmware/rover2_basic/` (flash with `./scripts/flash_firmware.sh`).

## Build (central-computer)

```bash
arduino-cli compile --fqbn arduino:avr:mega \
  --build-path /tmp/rover2_fw_build \
  ~/Documents/projects/rover2/firmware/rover2_basic
```

## Safety

- No motor movement in `setup()`.
- `detect` only on `{"cmd":"detect"}`.
- 5 s serial watchdog stops all motors.

See `docs/PROTOCOL.md` for JSON commands.
